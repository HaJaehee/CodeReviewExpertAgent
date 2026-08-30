"""리뷰 서비스 — MCP 도구가 부르는 실제 동작.

FastMCP 를 import 하지 않는다. 전송 계층(`crex/mcp.py`)과 분리해 두면 프로토콜
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
from .workspace import Workspace, WorkspaceError, switch

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
        workspace: Workspace | None = None,
    ) -> None:
        self.repo_root = repo_root
        self.config = config
        self.out_dir = out_dir or (repo_root / "reports")
        self.pipeline_factory = pipeline_factory
        #: 대상을 바꾸려면 어디서 왔는지 알아야 한다 (`set_workspace`).
        #: 없어도 동작한다 — 그때는 전환이 처음 정하는 것과 같아진다.
        self.workspace = workspace
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
            raise ReviewRequestError("from_ref 가 필요합니다")

        base = from_ref
        if use_merge_base:
            try:
                base = merge_base(self.repo_root, from_ref, to_ref)
                if base != from_ref:
                    log.info("분기점 사용: %s → %s", from_ref, base[:12])
            except GitError:
                # merge-base 가 실패하는 건 정상적인 경우도 있다 (커밋 해시 직접 지정 등).
                # 그때는 준 대로 비교한다.
                log.debug("merge-base 실패, %s 를 그대로 씁니다", from_ref)

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
            raise ReviewRequestError("path 가 필요합니다")

        expansion = _guard_paths(lambda: expand_paths(self.repo_root, [path], max_files=1))
        if not expansion.files:
            raise ReviewRequestError(
                f"{path} 는 리뷰 대상이 아닙니다 "
                f"(지원 언어가 아니거나 exclude 규칙에 걸림). {expansion.summary()}"
            )
        result = self.pipeline_factory(self.config).run_scan(expansion.files, self.repo_root)
        return self.summarize(result, "review_file")

    def review_directory(self, path: str, recursive: bool = True) -> str:
        """폴더 아래 지원 언어 파일을 전부 감사한다."""
        if not path:
            raise ReviewRequestError("path 가 필요합니다")

        expansion = _guard_paths(
            lambda: expand_paths(self.repo_root, [path], recursive=recursive)
        )
        if not expansion.files:
            raise ReviewRequestError(f"{path} 아래에 리뷰할 파일이 없습니다. {expansion.summary()}")

        log.info("%s 감사: %s", path, expansion.summary())
        result = self.pipeline_factory(self.config).run_scan(expansion.files, self.repo_root)
        return self.summarize(result, "review_directory")

    def set_workspace(self, path: str) -> str:
        """리뷰 대상 저장소를 바꾼다.

        이 프로세스에서만 바뀐다 — 설정 파일은 건드리지 않는다. 서버를 다시
        띄우면 원래 대상으로 돌아온다. 영구히 바꾸려면 `python -m crex workspace
        <경로>` 를 쓰거나 `crex.json` 의 `workspace` 를 고친다.

        에이전트가 부르는 도구다. 설정 파일을 조용히 고쳐 쓰는 쪽으로 만들면
        대화 한 번이 다음 사람의 실행 대상까지 바꿔 놓는다.
        """
        if not path or not path.strip():
            raise ReviewRequestError("path 가 필요합니다")

        previous = self.repo_root
        try:
            changed = switch(self.workspace, path.strip())
        except WorkspaceError as exc:
            raise ReviewRequestError(str(exc)) from exc

        self.workspace = changed
        self.repo_root = changed.root
        self.config = changed.config
        self.out_dir = changed.reports
        log.info("워크스페이스 변경: %s → %s", previous, changed.root)

        lines = [f"워크스페이스를 {changed.root} 로 바꿨습니다 (이전: {previous})."]
        if not changed.is_git:
            lines.append(
                "다만 .git 이 없습니다. diff 리뷰(review_diff/review_staged/"
                "review_working_tree)는 할 수 없고 파일·폴더 감사만 됩니다."
            )
        lines.append(f"설정: {changed.config.source or '기본값'}")
        lines.append(f"리포트: {changed.reports}")
        lines.append("이 서버가 살아 있는 동안만 유지됩니다. 설정 파일은 바뀌지 않았습니다.")
        return "\n".join(lines)

    def describe_workspace(self) -> str:
        """지금 무엇을 보고 있는지. 바꾸기 전에 확인용으로 부른다."""
        lines = [f"워크스페이스: {self.repo_root}"]
        if self.workspace is not None:
            lines[0] += f" (출처: {self.workspace.origin})"
            if not self.workspace.is_git:
                lines.append("경고: .git 이 없습니다 — diff 리뷰 불가, 파일·폴더 감사만 가능")
        lines.append(f"설정: {self.config.source or '기본값'}")
        lines.append(f"리포트: {self.out_dir}")
        return "\n".join(lines)

    # -- 내부 -------------------------------------------------------------

    def _review_diff_text(self, diff: str, paths: list[str] | None, tool: str) -> str:
        if not diff.strip():
            return "변경된 내용이 없습니다."
        result = self.pipeline_factory(self.config).run_diff(
            diff, self.repo_root, only_paths=paths
        )
        return self.summarize(result, tool)

    def summarize(self, result: ReviewResult, tool: str) -> str:
        report_path = self._write_report(result, tool)

        if not result.kept:
            # 에이전트는 이 요약만 보고 사용자에게 답한다. 고장을 "깨끗하다"로
            # 옮기지 않도록 첫 줄에서부터 구분한다.
            if result.healthy:
                lines = ["지적 사항 없음."]
            else:
                lines = [
                    f"리뷰가 온전히 끝나지 않았습니다 — 오류 {len(result.errors)}건 "
                    f"(생성 {result.generation_errors}, 검증 {result.verification_errors}). "
                    "지적 0건은 코드가 깨끗하다는 뜻이 아닙니다.",
                ]
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
