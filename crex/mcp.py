"""MCP 서버 — Zed 에이전트 패널에서 리뷰를 부르기 위한 얇은 바인딩.

    python -m crex.mcp

FastMCP 가 프로토콜을 담당한다. 타입 힌트와 docstring 에서 도구 스키마가
자동 생성되므로 여기서는 그것만 정확히 쓰면 된다.

**이 파일에는 로직이 없다.** 실제 동작은 `crex/service.py` 의 `ReviewService` 에
있고, 그쪽은 FastMCP 를 import 하지 않는다. 프로토콜 없이도 로직 전체를 테스트할
수 있고, MCP 사양이 바뀌어도 여기만 고치면 된다.

## 환경변수

| 변수 | 뜻 | 기본값 |
|---|---|---|
| `CREX_WORKSPACE` | 리뷰할 저장소 루트 | 현재 디렉터리에서 git 루트 탐색 |
| `CREX_REPO` | 위의 이전 이름. 계속 받는다 | — |
| `CREX_CONFIG` | 설정 파일 경로 | 워크스페이스 → 현재 디렉터리 순 탐색 |
| `CREX_REPORTS` | 리포트 저장 위치 | `<워크스페이스>/reports` |

MCP 서버는 저장소마다 하나씩 띄운다. Zed 프로젝트별로 `CREX_WORKSPACE` 를 주면
CREX 설치본은 한 벌만 두고 여러 저장소를 볼 수 있다. `settings.json` 예시는
`docs/operations.md` 의 Zed 연동 절에 있다.
"""

from __future__ import annotations

import logging
import os
import sys

# FastMCP 는 기동할 때 pypi.org 에 새 버전이 있는지 물어본다. 폐쇄망에서는
# 나가지도 못하고, 나가려 시도하는 것 자체가 반입 심사에서 걸린다.
# import 전에 꺼야 한다 — 설정이 import 시점에 읽힌다.
# 사용자가 기억해야 하는 환경변수로 두지 않고 여기서 못 박는다.
os.environ.setdefault("FASTMCP_CHECK_FOR_UPDATES", "off")

from fastmcp import FastMCP  # noqa: E402
from fastmcp.exceptions import ToolError  # noqa: E402

from .gitio import gitpython_available
from .service import MAX_SCAN_FILES, ReviewRequestError, ReviewService
from .workspace import resolve

log = logging.getLogger(__name__)

mcp = FastMCP(
    "crex",
    instructions=(
        "C++/C#/Python 코드 리뷰 도구다. 사용자가 변경사항 리뷰를 요청하면 "
        "review_staged 를, 브랜치 비교를 요청하면 review_diff 를 부른다. "
        "도구가 돌려준 요약을 그대로 전달하고, 지적 내용을 각색하거나 "
        "직접 지적을 추가하지 마라. 리뷰 판단은 도구가 한다."
    ),
)

#: 프로세스 수명 동안 유지되는 서비스. main() 에서 채운다.
_service: ReviewService | None = None


def service() -> ReviewService:
    if _service is None:
        raise ToolError("서버가 초기화되지 않았다")
    return _service


def _call(action) -> str:
    """서비스 호출을 감싸 사용자에게 보일 오류만 ToolError 로 올린다."""
    try:
        return action()
    except ReviewRequestError as exc:
        # 사용자가 고칠 수 있는 문제. 에이전트가 그대로 전달하면 된다.
        raise ToolError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        log.exception("리뷰 실패")
        raise ToolError(f"리뷰 실패 ({type(exc).__name__}): {exc}") from exc


# --------------------------------------------------------------------------
# 도구
# --------------------------------------------------------------------------


@mcp.tool
def review_staged(paths: list[str] | None = None) -> str:
    """스테이징된 변경(git diff --cached)을 리뷰한다. 커밋 전 자가 점검용이며 가장 자주 쓰인다.

    Args:
        paths: 저장소 루트 기준 상대 경로 목록. 파일 또는 폴더. 지정하면 그 경로의
            변경만 리뷰한다. 큰 변경에서 특정 모듈만 보고 싶을 때 쓴다.

    Returns:
        지적 요약과 전체 리포트 파일 경로. 지적이 없으면 그렇게 알린다.
    """
    return _call(lambda: service().review_staged(paths))


@mcp.tool
def review_working_tree(paths: list[str] | None = None) -> str:
    """아직 커밋하지 않은 모든 변경(git diff HEAD)을 리뷰한다.

    Args:
        paths: 저장소 루트 기준 상대 경로 목록. 지정하면 그 경로의 변경만 리뷰한다.

    Returns:
        지적 요약과 전체 리포트 파일 경로.
    """
    return _call(lambda: service().review_working_tree(paths))


@mcp.tool
def review_diff(
    from_ref: str,
    to_ref: str = "HEAD",
    paths: list[str] | None = None,
    use_merge_base: bool = True,
) -> str:
    """두 git 참조 사이의 변경을 리뷰한다. MR/PR 리뷰에 쓴다.

    Args:
        from_ref: 비교 시작 참조. 브랜치명(main), 태그, 커밋 해시.
        to_ref: 비교 끝 참조. 기본 HEAD.
        paths: 저장소 루트 기준 상대 경로 목록. 지정하면 그 경로의 변경만 리뷰한다.
        use_merge_base: 참으면 from_ref 대신 실제 분기점(merge-base)과 비교한다.
            브랜치가 오래됐을 때 남이 넣은 변경까지 딸려 들어오는 것을 막는다.
            특정 커밋 두 개를 그대로 비교하려면 거짓으로 준다.

    Returns:
        지적 요약과 전체 리포트 파일 경로.
    """
    return _call(lambda: service().review_diff(from_ref, to_ref, paths, use_merge_base=use_merge_base))


@mcp.tool
def review_file(path: str) -> str:
    """파일 하나를 통째로 감사한다. diff 가 아니라 기존 코드 전체를 본다.

    변경 라인이라는 제약이 없어 diff 리뷰보다 오탐이 많다. 레거시 코드를 살펴볼
    때 쓰고, 일상적인 변경 리뷰에는 review_staged 를 쓴다.

    Args:
        path: 저장소 루트 기준 상대 경로. C++/C#/Python 파일이어야 한다.

    Returns:
        지적 요약과 전체 리포트 파일 경로.
    """
    return _call(lambda: service().review_file(path))


@mcp.tool
def review_directory(path: str, recursive: bool = True) -> str:
    """폴더 아래 지원 언어 파일을 전부 감사한다.

    파일당 LLM 호출이 여러 번이라 비용이 크다. 대상이 상한을 넘으면 거부하고
    범위를 좁히라고 알린다.

    Args:
        path: 저장소 루트 기준 상대 경로.
        recursive: 하위 폴더까지 훑을지. 기본 참.

    Returns:
        지적 요약과 전체 리포트 파일 경로.
    """
    return _call(lambda: service().review_directory(path, recursive))


# --------------------------------------------------------------------------


def build_service() -> ReviewService:
    # 워크스페이스·설정·리포트 위치를 정하는 규칙은 CLI 와 완전히 같다.
    workspace = resolve()
    log.info("워크스페이스: %s", workspace.describe())
    return ReviewService(workspace.root, workspace.config, out_dir=workspace.reports)


def main(argv: list[str] | None = None) -> int:
    global _service

    # stdio 전송에서 stdout 은 JSON-RPC 전용이다. 로그가 한 줄이라도 섞이면
    # 스트림이 깨진다. FastMCP 가 stdout 을 잡기 전에 stderr 로 못 박아둔다.
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    try:
        _service = build_service()
    except (OSError, ValueError) as exc:
        print(f"설정 오류: {exc}", file=sys.stderr)
        return 2

    log.info(
        "MCP 서버 시작 — 저장소 %s, git=%s, %s",
        _service.repo_root,
        "GitPython" if gitpython_available() else "subprocess",
        _service.config.describe(),
    )
    log.info("폴더 감사 상한 %d개, 리포트 %s", MAX_SCAN_FILES, _service.out_dir)

    mcp.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
