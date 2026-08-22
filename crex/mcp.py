"""MCP server — a thin binding that lets an agent panel call the review pipeline.

    python -m crex.mcp                       # stdio (Zed, Claude Desktop, ...)
    python -m crex.mcp --transport http      # Streamable HTTP endpoint

FastMCP handles the protocol. Tool schemas are generated from the type hints and
docstrings below, so those two have to be exact — they are the only thing the agent
sees when it decides which tool to call.

**There is no logic in this file.** The real work lives in `ReviewService`
(`crex/service.py`), which does not import FastMCP. That split keeps the whole
behaviour testable without the protocol, and confines MCP spec changes to this file.

## Why the docstrings here are English

Everything else user-facing in CREX is Korean. These are not user-facing: tool
docstrings become the tool schema that the *agent* reads, alongside `AGENTS.md`,
which is English for the same reason. Comments meant for maintainers stay Korean.

## Environment variables

| Variable | Meaning | Default |
|---|---|---|
| `CREX_WORKSPACE` | Repository to review | git root found from the current directory |
| `CREX_REPO` | Former name of the above. Still accepted | — |
| `CREX_CONFIG` | Config file path | workspace, then current directory |
| `CREX_REPORTS` | Where reports are written | `<workspace>/reports` |

One server per repository is the normal setup: give each editor project its own
`CREX_WORKSPACE` and a single CREX install serves all of them. Zed `settings.json`
examples are in `docs/operations.md`.

## Transports

stdio is the default and the one to prefer — the editor spawns the process, talks
over stdin/stdout, and nothing listens on a port. Streamable HTTP exists for the
cases stdio cannot cover: one shared server for several people, or a client on a
different machine. It has no authentication, so bind it to loopback unless the
network in front of it is doing that job. See `docs/operations.md`.
"""

from __future__ import annotations

import argparse
import inspect
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

from . import __version__
from .gitio import gitpython_available
from .service import MAX_SCAN_FILES, ReviewRequestError, ReviewService
from .workspace import resolve

log = logging.getLogger(__name__)

#: HTTP 전송의 기본값. 관제 화면(18765) 옆자리를 쓴다.
DEFAULT_HTTP_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 18766
DEFAULT_HTTP_PATH = "/mcp"

#: 이 주소들에 바인드했을 때만 set_workspace 를 받는다.
LOOPBACK = ("127.0.0.1", "localhost", "::1")

#: 워크스페이스 변경을 허용할지. HTTP 로 원격에 열었으면 main() 에서 끈다.
_workspace_switchable = True

#: FastMCP 2.x 에는 `version` 인자가 없다. 있으면 채우고 없으면 넘긴다 —
#: 서버 버전을 알리자고 구버전에서 기동이 죽으면 곤란하다.
_VERSION_KWARG = (
    {"version": __version__}
    if "version" in inspect.signature(FastMCP.__init__).parameters
    else {}
)

mcp = FastMCP(
    "crex",
    **_VERSION_KWARG,
    instructions=(
        "Code review tool for C++, C# and Python. When the user asks for a review "
        "of their changes, call review_staged; when they ask to compare against a "
        "branch, call review_diff. Pass the returned summary through as-is: do not "
        "reword findings and do not add findings of your own — the judgement is the "
        "tool's, not yours. Check which repository is under review with "
        "get_workspace, and change it with set_workspace only when the user names "
        "another one."
    ),
)

#: 프로세스 수명 동안 유지되는 서비스. main() 에서 채운다.
_service: ReviewService | None = None


def service() -> ReviewService:
    if _service is None:
        raise ToolError("서버가 초기화되지 않았다")
    return _service


def _call(action) -> str:
    """Wrap a service call so only user-fixable problems surface as ToolError."""
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
    """Review the staged changes (git diff --cached).

    This is the self-check before committing, and the most common request by far.
    When a review request is ambiguous, call this one.

    Args:
        paths: Paths relative to the repository root — files or directories. Narrows
            the review to those paths, for when only one module of a large change
            matters.

    Returns:
        A summary of the findings and the path to the full report. Says so plainly
        when there is nothing to report.
    """
    return _call(lambda: service().review_staged(paths))


@mcp.tool
def review_working_tree(paths: list[str] | None = None) -> str:
    """Review every uncommitted change (git diff HEAD), staged or not.

    Args:
        paths: Paths relative to the repository root. Narrows the review to those paths.

    Returns:
        A summary of the findings and the path to the full report.
    """
    return _call(lambda: service().review_working_tree(paths))


@mcp.tool
def review_diff(
    from_ref: str,
    to_ref: str = "HEAD",
    paths: list[str] | None = None,
    use_merge_base: bool = True,
) -> str:
    """Review the changes between two git refs. This is the merge-request review.

    Args:
        from_ref: Ref to compare from — a branch name (main), tag, or commit hash.
        to_ref: Ref to compare to. Defaults to HEAD.
        paths: Paths relative to the repository root. Narrows the review to those paths.
        use_merge_base: When true, compares against the actual branch point
            (merge-base) rather than from_ref itself. This keeps other people's
            commits out of the review when the branch is behind. Pass false to
            compare two specific commits exactly as given.

    Returns:
        A summary of the findings and the path to the full report.
    """
    return _call(lambda: service().review_diff(from_ref, to_ref, paths, use_merge_base=use_merge_base))


@mcp.tool
def review_file(path: str) -> str:
    """Audit one whole file — existing code, not a diff.

    Without the "changed lines only" constraint this produces more false alarms than
    a diff review. Use it to get a feel for unfamiliar or legacy code; for everyday
    review of someone's work, use review_staged.

    Args:
        path: Path relative to the repository root. Must be a C++, C#, or Python file.

    Returns:
        A summary of the findings and the path to the full report.
    """
    return _call(lambda: service().review_file(path))


@mcp.tool
def review_directory(path: str, recursive: bool = True) -> str:
    """Audit every supported source file under a directory.

    Expensive: each file costs several LLM calls. If the directory holds more files
    than the limit allows, the tool refuses and tells you to narrow the scope rather
    than silently reviewing part of it.

    Args:
        path: Path relative to the repository root.
        recursive: Whether to descend into subdirectories. True by default.

    Returns:
        A summary of the findings and the path to the full report.
    """
    return _call(lambda: service().review_directory(path, recursive))


@mcp.tool
def get_workspace() -> str:
    """Report which repository is currently under review.

    Call this when the user asks what is being reviewed, and before concluding that
    a review looked at the wrong code — the paths in a report are relative to this
    repository.

    Returns:
        The workspace path, where that value came from, and the config and report
        locations in effect.
    """
    return _call(lambda: service().describe_workspace())


@mcp.tool
def set_workspace(path: str) -> str:
    """Change which repository the reviews run against.

    Call this only when the user names another repository. Do not switch because a
    review returned something unexpected — check with get_workspace first and tell
    the user what you found.

    The change lasts only while this server is running. The config file is not
    touched, so a restart goes back to the original target. Say that when you report
    the switch, so nobody assumes it is permanent.

    Args:
        path: Absolute path to the repository root — the directory holding `.git`.
            A subdirectory is accepted and promoted to the repository root.

    Returns:
        The new target and the config and report locations that followed it.
    """
    if not _workspace_switchable:
        # 전송 계층의 판단이라 여기에 둔다. 서비스는 누가 부르는지 모른다.
        raise ToolError(
            "이 서버는 루프백이 아닌 주소에 HTTP 로 열려 있어 워크스페이스를 바꿀 수 없다. "
            "대상 변경은 이 장비의 임의 디렉터리를 열 수 있게 하는 일이라, 인증 없는 "
            "원격 연결에서는 받지 않는다. CREX_WORKSPACE 를 주고 서버를 다시 띄우라."
        )
    return _call(lambda: service().set_workspace(path))


# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m crex.mcp", description="CREX MCP 서버"
    )
    parser.add_argument(
        "--transport", choices=("stdio", "http"), default="stdio",
        help="stdio(기본) — 에디터가 프로세스를 자식으로 띄운다. "
             "http — Streamable HTTP 엔드포인트를 연다",
    )
    parser.add_argument("--host", default=DEFAULT_HTTP_HOST,
                        help=f"http 전송의 바인드 주소 (기본 {DEFAULT_HTTP_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_HTTP_PORT,
                        help=f"http 전송의 포트 (기본 {DEFAULT_HTTP_PORT})")
    parser.add_argument("--path", default=DEFAULT_HTTP_PATH,
                        help=f"http 전송의 엔드포인트 경로 (기본 {DEFAULT_HTTP_PATH})")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def run_http(host: str, port: int, path: str) -> None:
    """Streamable HTTP 로 띄운다.

    전송 이름이 FastMCP 버전에 따라 다르다. 2.9 이후는 `"http"`, 그 이전은
    `"streamable-http"` 만 안다. 알 수 없는 이름이면 FastMCP 가 소켓을 잡기 *전에*
    ValueError 를 내므로, 그때만 옛 이름으로 다시 시도한다. 포트 충돌 같은 실패는
    OSError 라서 여기 걸리지 않는다 — 엉뚱한 재시도로 원인을 감추지 않는다.
    """
    try:
        mcp.run(transport="http", host=host, port=port, path=path)
    except ValueError as exc:
        if "transport" not in str(exc).lower():
            raise
        log.info("이 FastMCP 는 'http' 를 모른다. 'streamable-http' 로 다시 시도한다.")
        mcp.run(transport="streamable-http", host=host, port=port, path=path)


def build_service() -> ReviewService:
    # 워크스페이스·설정·리포트 위치를 정하는 규칙은 CLI 와 완전히 같다.
    workspace = resolve()
    log.info("워크스페이스: %s", workspace.describe())
    return ReviewService(
        workspace.root, workspace.config, out_dir=workspace.reports, workspace=workspace
    )


def main(argv: list[str] | None = None) -> int:
    global _service, _workspace_switchable

    args = build_parser().parse_args(argv)

    # stdio 전송에서 stdout 은 JSON-RPC 전용이다. 로그가 한 줄이라도 섞이면
    # 스트림이 깨진다. FastMCP 가 stdout 을 잡기 전에 stderr 로 못 박아둔다.
    # HTTP 에서도 그대로 stderr 를 쓴다 — 기동 스크립트가 둘을 달리 다루지 않게.
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    try:
        _service = build_service()
    except (OSError, ValueError) as exc:
        print(f"설정 오류: {exc}", file=sys.stderr)
        return 2

    log.info(
        "crex %s MCP 서버 시작 — 저장소 %s, git=%s, %s",
        __version__,
        _service.repo_root,
        "GitPython" if gitpython_available() else "subprocess",
        _service.config.describe(),
    )
    log.info("폴더 감사 상한 %d개, 리포트 %s", MAX_SCAN_FILES, _service.out_dir)

    if args.transport == "stdio":
        mcp.run()
        return 0

    _workspace_switchable = args.host in LOOPBACK
    log.info("Streamable HTTP — http://%s:%d%s", args.host, args.port, args.path)
    if not _workspace_switchable:
        log.warning(
            "%s 에 바인드한다. 이 엔드포인트에는 인증이 없다 — 붙을 수 있는 사람은 "
            "누구나 이 저장소의 소스를 리뷰에 태울 수 있다.", args.host
        )
        log.warning("원격 바인드이므로 set_workspace 를 막는다.")
    run_http(args.host, args.port, args.path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
