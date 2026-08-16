"""리뷰 서비스 — MCP 도구가 부르는 실제 동작.

FastMCP 를 import 하지 않는다. 전송 계층(`crx/mcp.py`)과 분리해 두면 프로토콜
없이도 로직 전체를 테스트할 수 있고, MCP 사양이 바뀌어도 여기는 안 건드린다.

## 요약만 돌려준다

MCP 도구의 반환값은 그대로 에이전트 컨텍스트로 들어간다. 지적 20건짜리 리포트를
통째로 넘기면 27B 모델의 컨텍스트가 리뷰 결과로 가득 찬다. 파이프라인 전체가
컨텍스트를 아끼는 설계인데 마지막에 흘리면 곤란하다.

전체 리포트는 파일로 쓰고, 여기서는 압축 요약만 돌려준다.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from .config import Config
from .gitio import GitError, diff_range, diff_staged, diff_working_tree, merge_base
from .paths import MAX_SCAN_FILES, TooManyFiles, expand_paths
from .pipeline import Pipeline
from .report import write_all
from .schema import ReviewResult

log = logging.getLogger(__name__)

#: 요약에 나열할 지적 개수 상한. 나머지는 파일에서 보게 한다.
SUMMARY_MAX_FINDINGS = 10

_SEVERITY_LABEL = {"high": "높음", "medium": "중간", "low": "낮음"}


class ReviewRequestError(ValueError):
    """사용자가 고칠 수 있는 요청 오류. 에이전트가 그대로 전달하면 된다."""


class ReviewService:
    def __init__(
        self,
        repo_root: Path,
        config: Config,
        *,
        out_dir: Path | None = None,
        pipeline_factory: Callable[[Config], Pipeline] = Pipeline,
    ) -> None:
        self.repo_root = repo_root
        self.config = config
        self.out_dir = out_dir or (repo_root / "reports")
        self.pipeline_factory = pipeline_factory
        self._seq = 0

    # -- 도구 -------------------------------------------------------------

    def review_diff(
        self,
        from_ref: str,
        to_ref: str = "HEAD",
        paths: list[str] | None = None,
        *,
        use_merge_base: bool = True,
    ) -> str:
        """두 참조 사이의 변경을 리뷰한다."""
        if not from_ref:
            raise ReviewRequestError("from_ref 가 필요하다")

        base = from_ref
        if use_merge_base:
            try:
                base = merge_base(self.repo_root, from_ref, to_ref)
                if base != from_ref:
                    log.info("분기점 사용: %s → %s", from_ref, base[:12])
            except GitError:
                # merge-base 가 실패하는 건 정상적인 경우도 있다 (커밋 해시 직접 지정 등).
                # 그때는 준 대로 비교한다.
                log.debug("merge-base 실패, %s 를 그대로 쓴다", from_ref)

        return self._review_diff_text(
            _guard_git(lambda: diff_range(self.repo_root, base, to_ref)),
            paths, "review_diff",
        )

    def review_staged(self, paths: list[str] | None = None) -> str:
        """스테이징된 변경을 리뷰한다."""
        return self._review_diff_text(
            _guard_git(lambda: diff_staged(self.repo_root)), paths, "review_staged"
        )

    def review_working_tree(self, paths: list[str] | None = None) -> str:
        """커밋하지 않은 모든 변경을 리뷰한다."""
        return self._review_diff_text(
            _guard_git(lambda: diff_working_tree(self.repo_root)), paths, "review_working_tree"
        )

    def review_file(self, path: str) -> str:
        """파일 하나를 통째로 감사한다."""
        if not path:
            raise ReviewRequestError("path 가 필요하다")

        expansion = _guard_paths(lambda: expand_paths(self.repo_root, [path], max_files=1))
        if not expansion.files:
            raise ReviewRequestError(
                f"{path} 는 리뷰 대상이 아니다 "
                f"(지원 언어가 아니거나 exclude 규칙에 걸림). {expansion.summary()}"
            )
        result = self.pipeline_factory(self.config).run_scan(expansion.files, self.repo_root)
        return self.summarize(result, "review_file")

    def review_directory(self, path: str, recursive: bool = True) -> str:
        """폴더 아래 지원 언어 파일을 전부 감사한다."""
        if not path:
            raise ReviewRequestError("path 가 필요하다")

        expansion = _guard_paths(
            lambda: expand_paths(self.repo_root, [path], recursive=recursive)
        )
        if not expansion.files:
            raise ReviewRequestError(f"{path} 아래에 리뷰할 파일이 없다. {expansion.summary()}")

        log.info("%s 감사: %s", path, expansion.summary())
        result = self.pipeline_factory(self.config).run_scan(expansion.files, self.repo_root)
        return self.summarize(result, "review_directory")

    # -- 내부 -------------------------------------------------------------

    def _review_diff_text(self, diff: str, paths: list[str] | None, tool: str) -> str:
        if not diff.strip():
            return "변경된 내용이 없다."
        result = self.pipeline_factory(self.config).run_diff(
            diff, self.repo_root, only_paths=paths
        )
        return self.summarize(result, tool)

    def summarize(self, result: ReviewResult, tool: str) -> str:
        report_path = self._write_report(result, tool)

        if not result.kept:
            lines = ["지적 사항 없음."]
            if result.errors:
                lines.append(f"다만 오류 {len(result.errors)}건이 있었다:")
                lines.extend(f"  - {e.splitlines()[0]}" for e in result.errors[:3])
            lines.append(f"청크 {result.chunks_reviewed}개 검토. 전체 리포트: {report_path}")
            return "\n".join(lines)

        counts: dict[str, int] = {}
        for finding in result.kept:
            counts[finding.severity.value] = counts.get(finding.severity.value, 0) + 1
        header = ", ".join(
            f"{_SEVERITY_LABEL[s]} {counts[s]}건"
            for s in ("high", "medium", "low")
            if s in counts
        )

        lines = [f"지적 {len(result.kept)}건 ({header})", ""]
        for index, finding in enumerate(result.kept[:SUMMARY_MAX_FINDINGS], 1):
            lines.append(
                f"{index}. {finding.path}:{finding.line} "
                f"[{finding.rule_id}] {_one_line(finding.message)}"
            )
        if len(result.kept) > SUMMARY_MAX_FINDINGS:
            lines.append(f"... 외 {len(result.kept) - SUMMARY_MAX_FINDINGS}건")

        lines.extend(["", f"전체 리포트: {report_path}"])
        return "\n".join(lines)

    def _write_report(self, result: ReviewResult, tool: str) -> Path:
        self._seq += 1
        try:
            return write_all(result, self.out_dir, stem=f"{tool}-{self._seq:03d}")["markdown"]
        except OSError as exc:
            # 리포트를 못 써도 요약은 돌려줘야 한다.
            log.warning("리포트 저장 실패: %s", exc)
            return Path(f"(저장 실패: {exc})")


# --------------------------------------------------------------------------


def _guard_git(action):
    try:
        return action()
    except GitError as exc:
        raise ReviewRequestError(str(exc)) from exc


def _guard_paths(action):
    try:
        return action()
    except TooManyFiles as exc:
        raise ReviewRequestError(str(exc)) from exc
    except FileNotFoundError as exc:
        raise ReviewRequestError(str(exc)) from exc
    except ValueError as exc:
        raise ReviewRequestError(str(exc)) from exc


def _one_line(text: str, limit: int = 120) -> str:
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


__all__ = [
    "MAX_SCAN_FILES",
    "ReviewRequestError",
    "ReviewService",
    "SUMMARY_MAX_FINDINGS",
]
