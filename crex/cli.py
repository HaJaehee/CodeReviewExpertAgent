"""CREX CLI.

    python -m crex review --from HEAD~1 --to HEAD
    python -m crex review --staged
    python -m crex scan src/buffer.cpp src/service.cs
    python -m crex doctor

CREX 를 리뷰 대상 저장소 안에 둘 필요는 없다. 작업 디렉터리는 CREX 루트로 두고
대상만 지정한다 — 우선순위와 이유는 `crex/workspace.py` 에 있다.

    python -m crex review --workspace D:\\work\\myrepo --staged
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .gitio import GitError, diff_range, diff_staged, diff_working_tree, gitpython_available
from .ground import GroundingGate
from .pipeline import Pipeline
from .report import to_markdown, write_all
from .rules import load_taxonomy
from .workspace import Workspace, resolve


def main(argv: list[str] | None = None) -> int:
    force_utf8_output()

    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    try:
        # 워크스페이스와 설정은 서로를 참조하므로 한 번에 정한다.
        workspace = resolve(getattr(args, "workspace", None), args.config)
    except (OSError, ValueError) as exc:
        print(f"설정 오류: {exc}", file=sys.stderr)
        return 2

    handlers = {"review": _cmd_review, "scan": _cmd_scan, "doctor": _cmd_doctor}
    return handlers[args.command](args, workspace)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m crex", description="폐쇄망 sLLM 코드리뷰")
    _add_global_args(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    # 전역 옵션을 서브커맨드에도 단다. argparse 기본 동작은 서브커맨드 *앞*에만
    # 허용하는데, 사람은 `crex review --staged --repo ...` 처럼 뒤에 쓴다.
    # SUPPRESS 를 써서 실제로 준 경우에만 앞의 값을 덮게 한다.
    common = argparse.ArgumentParser(add_help=False)
    _add_global_args(common, suppress=True)

    review = sub.add_parser("review", help="diff 를 리뷰한다", parents=[common])
    source = review.add_mutually_exclusive_group()
    source.add_argument("--staged", action="store_true", help="스테이징된 변경만")
    source.add_argument("--diff-file", type=Path, help="파일에서 unified diff 를 읽는다")
    review.add_argument("--from", dest="from_ref", default=None, help="비교 시작 ref")
    review.add_argument("--to", dest="to_ref", default="HEAD", help="비교 끝 ref")
    _add_output_args(review)

    scan = sub.add_parser("scan", help="파일 전체를 감사한다 (diff 없음)", parents=[common])
    scan.add_argument("paths", nargs="+", help="저장소 루트 기준 상대 경로")
    _add_output_args(scan)

    sub.add_parser("doctor", help="엔드포인트·분석기·택소노미 상태를 점검한다", parents=[common])
    return parser


def force_utf8_output() -> None:
    """stdout/stderr 를 UTF-8 로 맞춘다.

    CLI 와 테스트 러너가 공유한다 — `python tests/run_all.py` 는 폐쇄망 반입
    직후 처음 실행하는 명령이라 콘솔 인코딩으로 죽으면 안 된다.

    한국어 Windows 의 기본 콘솔 코드페이지는 cp949 다. 리포트에 심각도 표시
    이모지와 한글이 함께 들어가므로 그대로 두면 `python -m crex review` 가
    UnicodeEncodeError 로 죽는다. 대상 사용자가 바로 그 환경이라 반드시 터진다.

    파이프로 리다이렉트할 때도 같은 문제가 나므로 여기서 못 박는다.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            # 재설정이 불가능한 스트림(이미 감싸진 경우 등)은 그냥 둔다.
            pass


def _add_global_args(parser: argparse.ArgumentParser, *, suppress: bool = False) -> None:
    """어느 위치에서도 받는 옵션들.

    `suppress=True` 면 사용자가 실제로 지정했을 때만 네임스페이스에 들어간다.
    그래야 서브커맨드 파서가 앞에서 받은 값을 기본값으로 덮어쓰지 않는다.
    """
    default_config = argparse.SUPPRESS if suppress else None
    default_workspace = argparse.SUPPRESS if suppress else None
    default_verbose = argparse.SUPPRESS if suppress else False

    parser.add_argument("--config", type=Path, default=default_config,
                        help="설정 파일 경로 (기본: 워크스페이스 → 현재 디렉터리 순으로 탐색)")
    # --repo 는 예전 이름이다. 문서와 스크립트에 이미 퍼져 있어 계속 받는다.
    parser.add_argument("--workspace", "--repo", dest="workspace", type=Path,
                        default=default_workspace,
                        help="리뷰 대상 저장소 루트 (.git 이 있는 폴더). "
                             "생략하면 CREX_WORKSPACE → crex.toml 의 workspace → "
                             "현재 디렉터리 순으로 찾는다")
    parser.add_argument("-v", "--verbose", action="store_true", default=default_verbose)


def _add_output_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--out", type=Path, default=None, help="결과를 쓸 디렉터리 (미지정 시 stdout)")
    parser.add_argument("--stem", default="review", help="출력 파일명 접두어")


# --------------------------------------------------------------------------


def _cmd_review(args: argparse.Namespace, workspace: Workspace) -> int:
    diff_text = _collect_diff(args, workspace)
    if diff_text is None:
        return 2
    if not diff_text.strip():
        print("변경된 내용이 없다.", file=sys.stderr)
        return 0

    log = logging.getLogger(__name__)
    log.info("워크스페이스: %s", workspace.describe())
    log.info("설정: %s", workspace.config.describe())
    result = Pipeline(workspace.config).run_diff(diff_text, workspace.root)
    return _emit(result, args)


def _cmd_scan(args: argparse.Namespace, workspace: Workspace) -> int:
    log = logging.getLogger(__name__)
    log.info("워크스페이스: %s", workspace.describe())
    log.info("설정: %s", workspace.config.describe())
    result = Pipeline(workspace.config).run_scan(args.paths, workspace.root)
    return _emit(result, args)


def _cmd_doctor(args: argparse.Namespace, workspace: Workspace) -> int:
    """폐쇄망 반입 직후 무엇이 되고 무엇이 안 되는지 한 번에 보여준다."""
    config = workspace.config

    print(f"워크스페이스: {workspace.root}")
    print(f"  출처={workspace.origin} "
          f"git={'OK' if workspace.is_git else '없음 — diff 리뷰 불가, scan 만 가능'} "
          f"리포트={workspace.reports}\n")

    print(f"설정 파일: {config.source or '(없음 — 기본값 사용 중)'}")
    print(f"  {config.describe()}\n")

    ok = True

    print("택소노미")
    try:
        taxonomy = load_taxonomy(config.taxonomy_path) if config.taxonomy_path else load_taxonomy()
        print(f"  OK  v{taxonomy.version}, 룰 {len(taxonomy)}개")
    except (OSError, ValueError) as exc:
        print(f"  실패  {exc}")
        ok = False

    print("\nLLM 엔드포인트")
    for label, endpoint in (("생성", config.generator), ("검증", config.verifier)):
        client_ok, detail = _probe(endpoint)
        status = "OK " if client_ok else "실패"
        print(f"  {status} {label}: {endpoint.model} @ {endpoint.base_url}")
        print(f"       {detail}")
        ok = ok and client_ok

    print("\n정적분석 도구")
    gate = GroundingGate(cwd=workspace.root)
    for analyzer in gate.analyzers:
        available = analyzer.available()
        print(f"  {'OK ' if available else '없음'} {analyzer.name} ({analyzer.executable})")

    print("\ntree-sitter (선택)")
    for module in ("tree_sitter", "tree_sitter_cpp", "tree_sitter_c_sharp", "tree_sitter_python"):
        try:
            __import__(module)
            print(f"  OK  {module}")
        except ImportError:
            print(f"  없음 {module} — 휴리스틱 폴백으로 동작한다")

    print("\n런타임 (requirements.txt)")
    print(f"  {'OK ' if gitpython_available() else '없음'} GitPython"
          f"{'' if gitpython_available() else ' — subprocess 폴백으로 동작한다'}")
    try:
        import fastmcp

        print(f"  OK  fastmcp {getattr(fastmcp, '__version__', '')}")
    except ImportError:
        print("  없음 fastmcp — `python -m crex.mcp` 를 쓸 수 없다 (CLI 는 정상)")

    return 0 if ok else 1


def _probe(endpoint) -> tuple[bool, str]:
    from .llm import LLMClient

    return LLMClient(endpoint).health()


# --------------------------------------------------------------------------


def _collect_diff(args: argparse.Namespace, workspace: Workspace) -> str | None:
    if args.diff_file:
        try:
            return args.diff_file.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"diff 파일을 읽을 수 없다: {exc}", file=sys.stderr)
            return None

    if not workspace.is_git:
        print(f"{workspace.root} 는 git 저장소가 아니다 (.git 이 없다). "
              f"--workspace 로 프로젝트 루트를 지정하거나, diff 없이 볼 것이면 "
              f"scan 을 쓰라.", file=sys.stderr)
        return None

    try:
        if args.staged:
            return diff_staged(workspace.root)
        if args.from_ref:
            return diff_range(workspace.root, args.from_ref, args.to_ref)
        return diff_working_tree(workspace.root)
    except GitError as exc:
        print(f"{exc}", file=sys.stderr)
        return None


def _emit(result, args: argparse.Namespace) -> int:
    if args.out:
        paths = write_all(result, args.out, stem=args.stem)
        for kind, path in paths.items():
            print(f"{kind}: {path}")
    else:
        print(to_markdown(result))

    # 종료 코드로 CI 게이트를 걸 수 있게 한다.
    return 1 if any(f.severity.value == "high" for f in result.kept) else 0


if __name__ == "__main__":
    raise SystemExit(main())
