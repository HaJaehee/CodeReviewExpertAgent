"""파이프라인 오케스트레이션.

    diff → [청킹] → [정적분석 그라운딩] → RuleChecker(생성) → ReviewFilter(검증) → 결과

각 단계는 앞 단계의 실패를 흡수한다. 파일 하나를 못 읽어도, 분석기 하나가
없어도, 청크 하나의 LLM 호출이 실패해도 나머지는 계속 간다. 폐쇄망에서 부분
실패는 정상 상태다.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from pathlib import Path

from .chunk import Chunker, DiffSourceMismatch, SymbolLocator, parse_unified_diff
from .config import SEVERITY_ORDER, Config
from .filter import ReviewFilter
from .generate import RuleChecker
from .ground import (
    DEFAULT_ANALYZERS,
    OPTIONAL_ANALYZERS,
    Analyzer,
    ClangTidy,
    GroundingGate,
    Roslynator,
    RoslynAnalyzers,
    Semgrep,
)
from .llm import LLMClient
from .paths import filter_file_diffs
from .rules import Taxonomy, load_taxonomy
from .schema import DiffLine, FileDiff, Language, LineStatus, ReviewChunk, ReviewResult

log = logging.getLogger(__name__)


class Pipeline:
    def __init__(self, config: Config, taxonomy: Taxonomy | None = None) -> None:
        self.config = config
        if taxonomy is not None:
            self.taxonomy = taxonomy
        elif config.taxonomy_path:
            self.taxonomy = load_taxonomy(config.taxonomy_path)
        else:
            self.taxonomy = load_taxonomy()
        self.generator = LLMClient(config.generator)
        self.verifier = LLMClient(config.verifier)

    # -- 진입점 -------------------------------------------------------------

    def run_diff(
        self, diff_text: str, repo_root: Path, *, only_paths: list[str] | None = None
    ) -> ReviewResult:
        """unified diff 를 리뷰한다. 주 사용 경로.

        `only_paths` 를 주면 그 파일·폴더의 변경만 본다. 큰 MR 에서 특정 모듈만
        보고 싶을 때 쓴다.
        """
        result = ReviewResult()

        with self._timed(result, "chunk"):
            file_diffs = parse_unified_diff(diff_text)
            if only_paths:
                before = len(file_diffs)
                file_diffs = filter_file_diffs(file_diffs, only_paths)
                log.info("경로 필터: 파일 %d개 → %d개", before, len(file_diffs))
            chunks = self._chunk_diff(file_diffs, repo_root, result)

        return self._review(chunks, repo_root, result, require_changed_line=self.config.review.require_changed_line)

    def run_scan(self, paths: list[str], repo_root: Path) -> ReviewResult:
        """전체 파일 감사. diff 없이 기존 코드를 통째로 본다."""
        result = ReviewResult()

        with self._timed(result, "chunk"):
            chunks: list[ReviewChunk] = []
            for path in paths:
                source = _read(repo_root / path, result)
                if source is None:
                    continue
                chunks.extend(self._chunk_whole_file(path, source))

        # 전체 감사에서는 '변경된 라인' 개념이 없으므로 제약을 푼다.
        return self._review(chunks, repo_root, result, require_changed_line=False)

    # -- 단계 ---------------------------------------------------------------

    def _review(
        self,
        chunks: list[ReviewChunk],
        repo_root: Path,
        result: ReviewResult,
        *,
        require_changed_line: bool,
    ) -> ReviewResult:
        result.chunks_reviewed = len(chunks)
        if not chunks:
            log.info("리뷰할 청크가 없다")
            return result

        log.info("청크 %d개 생성", len(chunks))

        if self.config.grounding.enabled:
            with self._timed(result, "ground"):
                gate = self._build_gate(repo_root)
                paths = sorted({c.path for c in chunks})
                findings = gate.collect(paths)
                GroundingGate.attach(chunks, findings)
                result.static_findings_count = len(findings)
                log.info("정적분석 %d건:\n%s", len(findings), gate.report())

        with self._timed(result, "generate"):
            checker = RuleChecker(
                self.generator,
                self.taxonomy,
                max_findings=self.config.review.max_findings_per_chunk,
                max_workers=self.config.review.max_workers,
                restrict_lines=require_changed_line,
            )
            raw = checker.review(chunks)
            result.generation_errors = len(checker.errors)
            result.errors.extend(checker.errors)
            log.info("생성된 지적 %d건", len(raw))
            if checker.errors and not raw:
                # 이 조합이 사고의 정체였다. 로그에만 남기면 "깨끗한 코드"로 읽힌다.
                log.error(
                    "청크 %d개가 모두 생성에 실패했다 — 0건은 코드가 깨끗해서가 아니다. "
                    "`python -m crex doctor` 로 구조화 출력을 점검하라.",
                    len(checker.errors),
                )

        with self._timed(result, "filter"):
            review_filter = ReviewFilter(
                self.verifier,
                {c.chunk_id: c for c in chunks},
                require_changed_line=require_changed_line,
                max_workers=self.config.review.max_workers,
            )
            kept, rejected = review_filter.filter(raw)
            result.verification_errors = len(review_filter.errors)
            result.errors.extend(review_filter.errors)
            if review_filter.errors and not kept:
                log.error(
                    "지적 %d건이 전부 검증 호출 실패로 기각됐다 — 검증 엔드포인트를 점검하라.",
                    len(review_filter.errors),
                )

        result.kept = _sort_findings(self._apply_min_severity(kept))
        result.rejected = rejected
        return result

    def _apply_min_severity(self, findings: list) -> list:
        """설정된 최소 심각도 미만은 리포트에서 뺀다.

        기각(rejected)과는 다르다. 이건 '유효하지만 지금은 보고 싶지 않은' 것이다.
        룰별 정밀도를 아직 모를 때 high 만 켜 두고, 측정 후 낮추는 용도로 쓴다.
        """
        threshold = SEVERITY_ORDER[self.config.review.min_severity]
        if threshold == 0:
            return findings
        keep = [f for f in findings if SEVERITY_ORDER[f.severity.value] >= threshold]
        dropped = len(findings) - len(keep)
        if dropped:
            log.info(
                "min_severity=%s 로 %d건 숨김", self.config.review.min_severity, dropped
            )
        return keep

    def _chunk_diff(
        self, file_diffs: list[FileDiff], repo_root: Path, result: ReviewResult
    ) -> list[ReviewChunk]:
        chunker = Chunker(
            expansion_limit=self.config.chunking.expansion_limit,
            expansion_truncate=self.config.chunking.expansion_truncate,
            absolute_max_lines=self.config.chunking.absolute_max_lines,
            on_mismatch=self.config.chunking.on_mismatch,
        )

        chunks: list[ReviewChunk] = []
        for file_diff in file_diffs:
            if file_diff.language is Language.OTHER:
                log.debug("%s: 지원하지 않는 언어, 건너뜀", file_diff.path)
                continue
            source = _read(repo_root / file_diff.path, result)
            if source is None:
                continue
            try:
                chunks.extend(chunker.chunk_file(file_diff, source))
            except DiffSourceMismatch as exc:
                # 라인 번호가 밀린 채로 리뷰하면 전부 거짓말이 된다. 파일 단위로 건너뛴다.
                result.errors.append(str(exc))
                log.error("%s 건너뜀: diff/파일 불일치", file_diff.path)
        return chunks

    def _chunk_whole_file(self, path: str, source: str) -> list[ReviewChunk]:
        """전체 파일을 심볼 경계에 맞춰 창으로 자른다."""
        language = Language.from_path(path)
        if language is Language.OTHER:
            return []

        lines = source.splitlines()
        if not lines:
            return []

        window = self.config.chunking.absolute_max_lines
        locator = SymbolLocator()
        chunks: list[ReviewChunk] = []
        start = 1
        index = 0

        while start <= len(lines):
            end = min(start + window - 1, len(lines))
            # 창의 끝이 심볼 중간을 자르면 심볼 경계까지만 물러난다.
            if end < len(lines):
                symbol = locator.locate(lines, language, end, end)
                if symbol and symbol.start > start:
                    end = symbol.start - 1

            body = [
                DiffLine(LineStatus.UNCHANGED, n, lines[n - 1])
                for n in range(start, end + 1)
            ]
            chunks.append(
                ReviewChunk(
                    chunk_id=f"{path}#{index}",
                    path=path,
                    language=language,
                    start_line=start,
                    end_line=end,
                    lines=body,
                    changed_linenos=set(range(start, end + 1)),
                )
            )
            index += 1
            start = end + 1

        return chunks

    def _build_gate(self, repo_root: Path) -> GroundingGate:
        grounding = self.config.grounding
        self._repo_root = repo_root
        instances: list[Analyzer] = []
        seen: set[type[Analyzer]] = set()

        for classes in DEFAULT_ANALYZERS.values():
            for cls in classes:
                if cls in seen:
                    continue
                seen.add(cls)
                if grounding.analyzers and cls.name not in grounding.analyzers:
                    continue
                instances.append(self._instantiate(cls, grounding.timeout))

        # 이름을 명시해야만 켜지는 것들. semgrep 은 룰팩 경로가 있으면 이름 없이도 켠다.
        for name, cls in OPTIONAL_ANALYZERS.items():
            if cls in seen:
                continue
            wanted = name in grounding.analyzers
            if cls is Semgrep and not grounding.analyzers and grounding.semgrep_config:
                wanted = True
            if not wanted:
                continue
            if cls is Semgrep and not grounding.semgrep_config:
                log.warning(
                    "semgrep 을 요청했으나 grounding.semgrep_config 가 없다. "
                    "폐쇄망에서 'auto' 는 동작하지 않으므로 건너뛴다."
                )
                continue
            seen.add(cls)
            instances.append(self._instantiate(cls, grounding.timeout))

        return GroundingGate(instances, cwd=repo_root, max_workers=self.config.review.max_workers)

    def _instantiate(self, cls: type[Analyzer], timeout: float) -> Analyzer:
        grounding = self.config.grounding
        if cls is ClangTidy:
            return ClangTidy(
                checks=grounding.clang_tidy_checks,
                compile_commands_dir=grounding.compile_commands_dir,
                project_root=self._repo_root,
                timeout=timeout,
            )
        if cls is RoslynAnalyzers:
            return RoslynAnalyzers(project=grounding.dotnet_project, timeout=timeout)
        if cls is Roslynator:
            return Roslynator(project=grounding.dotnet_project, timeout=timeout)
        if cls is Semgrep:
            return Semgrep(config=grounding.semgrep_config or "auto", timeout=timeout)
        return cls(timeout=timeout)

    # -- 계측 ---------------------------------------------------------------

    @contextmanager
    def _timed(self, result: ReviewResult, name: str):
        """단계 하나의 소요 시간을 잰다.

        모듈 함수가 아니라 메서드인 이유: 이 구문이 파이프라인의 유일한 단계
        경계다. 하위 클래스가 여기만 감싸면 단계 시작·종료를 그대로 관찰할 수
        있고, `_review` 를 복제할 필요가 없다 (`crex/viz/engine.py`).
        """
        started = time.perf_counter()
        try:
            yield
        finally:
            result.timings[name] = time.perf_counter() - started


# --------------------------------------------------------------------------


def _read(path: Path, result: ReviewResult) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        message = f"{path} 를 읽을 수 없다: {exc}"
        result.errors.append(message)
        log.warning("%s", message)
        return None


def _sort_findings(findings: list) -> list:
    """심각도 높은 순 → 파일 → 라인 순. 리뷰어가 위에서부터 읽으면 되도록."""
    return sorted(
        findings,
        key=lambda f: (-SEVERITY_ORDER[f.severity.value], f.path, f.line),
    )
