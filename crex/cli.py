"""CREX CLI.

    python -m crex review --from HEAD~1 --to HEAD
    python -m crex review --staged
    python -m crex scan src/buffer.cpp src/service.cs
    python -m crex compiledb
    python -m crex doctor
    python -m crex workspace D:\\work\\myrepo

CREX 를 리뷰 대상 저장소 안에 둘 필요는 없다. 작업 디렉터리는 CREX 루트로 두고
대상만 지정한다 — 우선순위와 이유는 `crex/workspace.py` 에 있다.

    python -m crex review --workspace D:\\work\\myrepo --staged
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import __version__
from .compiledb import CompileDbError, DEFAULT_TARGET, generate
from .config import DEFAULT_CONFIG_NAMES, find_config
from .gitio import GitError, diff_range, diff_staged, diff_working_tree, gitpython_available
from .filter import VERDICT_SCHEMA
from .generate import build_findings_schema
from .ground import GroundingGate
from .pipeline import Pipeline
from .report import to_markdown, write_all
from .rules import load_taxonomy
from .workspace import (
    Workspace,
    persist_compile_commands_dir,
    persist_workspace,
    resolve,
)


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

    handlers = {
        "review": _cmd_review,
        "scan": _cmd_scan,
        "compiledb": _cmd_compiledb,
        "doctor": _cmd_doctor,
        "workspace": _cmd_workspace,
    }
    return handlers[args.command](args, workspace)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m crex", description="폐쇄망 sLLM 코드리뷰")
    parser.add_argument("--version", action="version", version=f"crex {__version__}")
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

    compiledb = sub.add_parser(
        "compiledb",
        help="compile_commands.json 을 만들고 crex.toml 에 적는다 (C++ clang-tidy 용)",
        parents=[common],
    )
    compiledb.add_argument("--project", type=Path, default=None,
                           help="대상 .sln/.vcxproj/CMakeLists.txt. 생략하면 워크스페이스에서 찾는다")
    compiledb.add_argument("--configuration", default="Debug", help="MSBuild 구성 (기본 Debug)")
    compiledb.add_argument("--platform", default="x64", help="MSBuild 플랫폼 (기본 x64)")
    compiledb.add_argument("--target", default=DEFAULT_TARGET,
                           help="MSBuild 타깃 (기본 Rebuild — 증분 빌드는 반쪽짜리 DB 를 만든다)")
    compiledb.add_argument("--generator", default="Ninja", help="CMake 제너레이터 (기본 Ninja)")
    compiledb.add_argument("--out-dir", type=Path, default=None,
                           help="산출물 위치 (기본 <워크스페이스>/.crex/compiledb)")
    compiledb.add_argument("--msbuild-arg", action="append", default=[], dest="extra_args",
                           metavar="ARG", help="빌드 도구에 그대로 넘길 인자. 여러 번 쓸 수 있다")
    compiledb.add_argument("--no-save", action="store_true",
                           help="crex.toml 에 compile_commands_dir 를 적지 않는다")

    sub.add_parser("doctor", help="엔드포인트·분석기·택소노미 상태를 점검한다", parents=[common])

    workspace = sub.add_parser(
        "workspace", help="리뷰 대상 저장소를 확인하거나 crex.toml 에 고정한다", parents=[common]
    )
    workspace.add_argument("path", nargs="?", default=None,
                           help="새 워크스페이스 경로. 생략하면 현재 상태만 보여준다")
    workspace.add_argument("--clear", action="store_true",
                           help="crex.toml 에서 workspace 키를 지운다")
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


def _cmd_workspace(args: argparse.Namespace, workspace: Workspace) -> int:
    """리뷰 대상을 확인하고, `crex.toml` 에 고정한다.

    `--workspace` 는 그 실행에만 적용된다. 매번 치지 않으려면 어딘가에 적어야
    하는데, 그 "어딘가"를 사람이 직접 찾아 열게 하지 않는다.
    """
    if args.path and args.clear:
        print("경로와 --clear 를 같이 줄 수 없다.", file=sys.stderr)
        return 2

    if not args.path and not args.clear:
        print(f"워크스페이스: {workspace.root}")
        print(f"  출처={workspace.origin} "
              f"git={'OK' if workspace.is_git else '없음 — diff 리뷰 불가, scan 만 가능'}")
        print(f"설정 파일: {workspace.config.source or '(없음 — 기본값 사용 중)'}")
        print(f"리포트: {workspace.reports}")
        print()
        print("고정하려면: python -m crex workspace <경로>")
        return 0

    target = _config_to_write(args)
    try:
        if args.clear:
            persist_workspace(target, None)
            print(f"{target} 에서 workspace 키를 지웠다.")
            print("이제 현재 디렉터리에서 git 루트를 찾는다.")
            return 0

        # 쓰기 전에 검증한다. 없는 경로를 설정 파일에 박아두면 다음 실행이 죽는다.
        resolved = resolve(args.path)
        persist_workspace(target, resolved.root)
    except (OSError, ValueError) as exc:
        print(f"{exc}", file=sys.stderr)
        return 2

    print(f"워크스페이스를 {resolved.root} 로 고정했다.")
    print(f"  기록한 파일: {target}")
    if not resolved.is_git:
        print("  경고: .git 이 없다 — review 는 못 하고 scan 만 된다.")
    return 0


def _config_to_write(args: argparse.Namespace) -> Path:
    """어느 설정 파일에 적을 것인가.

    `--config` 로 지정했으면 그 파일. 아니면 **현재 디렉터리 기준**으로 찾은
    파일이다 — 워크스페이스 안에서 발견된 설정에 적으면, 그 저장소를 떠나는
    순간 방금 한 설정이 사라진다. 아무것도 없으면 여기에 새로 만든다.
    """
    if args.config:
        return Path(args.config)
    found = find_config(Path.cwd())
    return found if found is not None else Path.cwd() / DEFAULT_CONFIG_NAMES[0]


def _cmd_compiledb(args: argparse.Namespace, workspace: Workspace) -> int:
    """compile_commands.json 을 만들고, 만든 자리를 설정에 적는다.

    두 번째 절반이 핵심이다. 파일만 만들어 주고 "이제 crex.toml 을 여세요" 하면
    거기서 절반이 떨어져 나간다 — 그래서 clang-tidy 가 반쯤 눈을 감은 채로
    돌던 것이다.
    """
    try:
        result = generate(
            workspace.root,
            project=args.project,
            configuration=args.configuration,
            platform=args.platform,
            target=args.target,
            generator=args.generator,
            output_dir=args.out_dir,
            extra_args=tuple(args.extra_args),
        )
    except CompileDbError as exc:
        print(f"{exc}", file=sys.stderr)
        return 2

    print(f"\n{result.json_path}")
    print(f"  대상   : {result.project.describe()}")
    print(f"  엔트리 : {result.entries}개")

    if result.entries == 0:
        # 파일은 생겼는데 내용이 없다. 이걸 설정에 적으면 '설정했는데 왜 안 되지'가 된다.
        print("\n비어 있다. 설정에 적지 않는다.", file=sys.stderr)
        print("  - 증분 빌드였을 수 있다. 이미 최신인 파일은 기록되지 않는다.", file=sys.stderr)
        print("  - --configuration/--platform 이 실제 빌드되는 조합인지 확인하라.", file=sys.stderr)
        if result.log_path:
            print(f"  - 빌드 로그: {result.log_path}", file=sys.stderr)
        return 1

    value = _config_value(result.directory, workspace.root)

    if args.no_save:
        print("\n설정에 적으려면 --no-save 를 빼고 다시 돌리거나, 직접 적으라:")
        print(f'  [grounding]\n  compile_commands_dir = "{value.as_posix()}"')
        return 0

    target = _workspace_config_to_write(args, workspace)
    try:
        persist_compile_commands_dir(target, value)
    except (OSError, ValueError) as exc:
        print(f"설정에 적지 못했다: {exc}", file=sys.stderr)
        return 2

    print(f"  기록   : {target}")
    if not target.resolve().is_relative_to(workspace.root.resolve()):
        print("  참고: 이 설정 파일은 워크스페이스 밖에 있다. 저장소마다 다른 값이므로, "
              "여러 저장소를 이 파일 하나로 쓰고 있다면 "
              f"{workspace.root / DEFAULT_CONFIG_NAMES[0]} 로 옮기라.")
    return 0


def _config_value(directory: Path, root: Path) -> Path:
    """설정에 적을 값. 워크스페이스 안이면 상대경로로 적는다.

    분석기는 워크스페이스를 cwd 로 실행되므로 상대경로가 그대로 맞고, 저장소를
    다른 장비에 클론해도 값이 살아남는다.
    """
    try:
        return directory.resolve().relative_to(root.resolve())
    except ValueError:
        return directory.resolve()


def _workspace_config_to_write(args: argparse.Namespace, workspace: Workspace) -> Path:
    """어느 설정 파일에 적을 것인가.

    `workspace` 명령과 반대다. compile_commands_dir 는 **저장소마다 다른 값**이라
    지금 쓰이고 있는 설정 파일에 적고, 없으면 워크스페이스 안에 새로 만든다.
    CREX 루트에 만들면 다음 저장소를 리뷰할 때 엉뚱한 경로를 가리킨다.
    """
    if args.config:
        return Path(args.config)
    if workspace.config.source is not None:
        return workspace.config.source
    return workspace.root / DEFAULT_CONFIG_NAMES[0]


def _cmd_doctor(args: argparse.Namespace, workspace: Workspace) -> int:
    """폐쇄망 반입 직후 무엇이 되고 무엇이 안 되는지 한 번에 보여준다."""
    config = workspace.config

    print(f"crex {__version__}\n")
    print(f"워크스페이스: {workspace.root}")
    print(f"  출처={workspace.origin} "
          f"git={'OK' if workspace.is_git else '없음 — diff 리뷰 불가, scan 만 가능'} "
          f"리포트={workspace.reports}\n")

    print(f"설정 파일: {config.source or '(없음 — 기본값 사용 중)'}")
    print(f"  {config.describe()}\n")

    ok = True

    print("택소노미")
    taxonomy = None
    try:
        taxonomy = load_taxonomy(config.taxonomy_path) if config.taxonomy_path else load_taxonomy()
        print(f"  OK  v{taxonomy.version}, 룰 {len(taxonomy)}개")
    except (OSError, ValueError) as exc:
        print(f"  실패  {exc}")
        ok = False

    print("\nLLM 엔드포인트")
    # 연결만 보지 않는다. 리뷰가 실제로 보내는 스키마를 그대로 보내본다 —
    # guided decoding 이 막혀 있으면 연결은 멀쩡한데 지적만 0건이 되고,
    # 그 상태를 "OK" 로 보고하던 것이 이 점검을 만든 이유다.
    for label, endpoint, schemas in (
        ("생성", config.generator, [("findings", _sample_findings_schema(taxonomy))]),
        ("검증", config.verifier, [("verdict", VERDICT_SCHEMA)]),
    ):
        print(f"  {label}: {endpoint.model} @ {endpoint.base_url}")
        for step in _probe(endpoint, schemas):
            print(f"    {'OK  ' if step.ok else '실패'} {step.label}")
            print(f"         {step.detail}")
            ok = ok and step.ok

    print("\n정적분석 도구")
    gate = GroundingGate(cwd=workspace.root)
    for analyzer in gate.analyzers:
        available = analyzer.available()
        print(f"  {'OK ' if available else '없음'} {analyzer.name} ({analyzer.executable})")

    print("\ncompile_commands.json (C++ clang-tidy)")
    print(f"  {_compiledb_status(config.grounding.compile_commands_dir, workspace.root)}")

    print("\n빌드 대상 (C# roslyn)")
    print(f"  {_dotnet_project_status(config.grounding.dotnet_project, workspace.root)}")

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


def _compiledb_status(configured: str | None, root: Path) -> str:
    """설정과 실제 파일을 함께 본다. 둘 중 하나만 맞아도 clang-tidy 는 눈을 감는다."""
    if not configured:
        return "없음 — `python -m crex compiledb` 로 만들 수 있다 (C++ 이 아니면 무시하라)"

    directory = Path(configured)
    if not directory.is_absolute():
        directory = root / directory
    path = directory / "compile_commands.json"
    if not path.is_file():
        return f"실패  {path} 가 없다 — 경로가 맞는지 확인하거나 다시 만들라"
    try:
        entries = len(json.loads(path.read_text(encoding="utf-8", errors="replace")))
    except (OSError, ValueError) as exc:
        return f"실패  {path} 를 읽지 못했다: {exc}"
    return f"OK  {path} (엔트리 {entries}개)"


def _dotnet_project_status(configured: str | None, root: Path) -> str:
    """roslyn 은 프로젝트를 빌드해야 도는데, 그 대상이 정해지는지를 미리 보여준다.

    리뷰 때는 바뀐 파일이 속한 프로젝트를 먼저 찾는다. 여기서는 그 정보가 없으니
    솔루션/단일 프로젝트 폴백만 확인한다 — 그것도 안 되면 리뷰에서도 위태롭다.
    """
    from .ground import find_dotnet_project

    if configured:
        path = Path(configured)
        if not path.is_absolute():
            path = root / path
        if not path.exists():
            return f"실패  {path} 가 없다 — grounding.dotnet_project 를 확인하라"
        return f"OK  {path} (dotnet_project 설정)"

    found = find_dotnet_project(root)
    if found is None:
        return ("없음 — 자동으로 정할 수 없다. C# 을 리뷰한다면 "
                "grounding.dotnet_project 를 지정하라 (C# 이 아니면 무시하라)")
    return f"OK  {found} (자동 탐색)"


def _probe(endpoint, schemas):
    from .llm import LLMClient

    return LLMClient(endpoint).probe(schemas)


def _sample_findings_schema(taxonomy) -> dict:
    """생성 단계가 실제로 보내는 것과 같은 모양의 스키마.

    라인 enum 과 룰 ID enum 이 들어 있어야 의미가 있다. 그 둘이 이 시스템에서
    환각을 막는 유일한 구조적 장치이고, 서버가 그것을 강제하지 못하면
    파이프라인의 전제가 무너진다.
    """
    # 택소노미를 못 읽었어도 점검은 계속한다 — 그쪽 실패는 이미 위에서 보고했다.
    rule_ids = sorted(taxonomy.valid_ids())[:8] if taxonomy else ["cpp.use-after-move"]
    return build_findings_schema(rule_ids=rule_ids, allowed_lines=[41, 42], max_findings=2)


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

    if not result.healthy:
        # 파이프라인이 끝까지 못 갔으면 "지적 없음"으로 통과시키면 안 된다.
        # high 지적(1)과 구분되는 코드를 써서 CI 가 둘을 다르게 다룰 수 있게 한다.
        print(
            f"\n경고: 오류 {len(result.errors)}건으로 리뷰가 온전히 끝나지 않았다 "
            f"(생성 {result.generation_errors}, 검증 {result.verification_errors}).\n"
            "      `python -m crex doctor` 로 엔드포인트를 점검하라.",
            file=sys.stderr,
        )
        for message in result.errors[:3]:
            print(f"      - {message.splitlines()[0]}", file=sys.stderr)
        return 3

    # 종료 코드로 CI 게이트를 걸 수 있게 한다.
    return 1 if any(f.severity.value == "high" for f in result.kept) else 0


if __name__ == "__main__":
    raise SystemExit(main())
