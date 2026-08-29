"""CLI 인자 처리와 출력 인코딩 검증.

둘 다 개발 중 실제로 물렸던 것들이라 회귀 방지가 목적이다.
"""

from __future__ import annotations

import ast
import inspect
import io
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crex import __version__  # noqa: E402
from crex.cli import _build_parser, _emit, force_utf8_output  # noqa: E402
from crex.report import to_markdown, to_sarif  # noqa: E402
from crex.viz.api import Context  # noqa: E402
from crex.schema import Dimension, Finding, ReviewResult, Severity  # noqa: E402


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def test_global_flags_accepted_after_subcommand() -> None:
    """`crex review --staged --workspace X` 가 동작해야 한다.

    argparse 기본 동작은 전역 옵션을 서브커맨드 *앞*에만 허용한다. 사람은
    자연스럽게 뒤에 쓰고, 문서에도 그렇게 적힌 예시가 있었다.
    """
    parser = _build_parser()

    after = parser.parse_args(
        ["review", "--staged", "--workspace", "/tmp/x", "--config", "/tmp/c.toml"]
    )
    _check(str(after.workspace) in ("/tmp/x", "\\tmp\\x"), f"workspace: {after.workspace}")
    _check(after.config is not None and "c.toml" in str(after.config), f"config: {after.config}")
    _check(after.staged, "staged 플래그 손실")

    before = parser.parse_args(["--workspace", "/tmp/x", "review", "--staged"])
    _check(str(before.workspace) in ("/tmp/x", "\\tmp\\x"), f"workspace: {before.workspace}")


def test_repo_is_accepted_as_alias() -> None:
    """`--repo` 는 예전 이름이다. 문서와 스크립트에 퍼져 있어 계속 받아야 한다."""
    args = _build_parser().parse_args(["review", "--repo", "/tmp/x"])
    _check(str(args.workspace) in ("/tmp/x", "\\tmp\\x"), f"workspace: {args.workspace}")


def test_subcommand_flag_wins_over_global() -> None:
    """양쪽에 주면 서브커맨드 쪽(나중에 쓴 것)이 이겨야 한다."""
    args = _build_parser().parse_args(
        ["--workspace", "/tmp/a", "review", "--workspace", "/tmp/b"]
    )
    _check(str(args.workspace).endswith("b"), f"workspace: {args.workspace}")


def test_workspace_defaults_to_none() -> None:
    """지정하지 않으면 None 이어야 한다.

    예전에는 기본값이 `Path.cwd()` 여서, 설정 파일의 `workspace` 나 환경변수가
    있어도 '사용자가 현재 디렉터리를 명시했다'와 구분되지 않았다.
    """
    args = _build_parser().parse_args(["review", "--staged"])
    _check(args.workspace is None, f"workspace: {args.workspace}")


def test_global_flag_not_clobbered_by_subparser_default() -> None:
    """서브커맨드에서 안 주면 앞에서 준 값이 살아 있어야 한다.

    SUPPRESS 를 빠뜨리면 서브파서 기본값이 앞의 값을 덮어써서 조용히 무시된다.
    """
    args = _build_parser().parse_args(["--config", "/tmp/c.toml", "review"])
    _check(args.config is not None, "전역 --config 가 서브파서 기본값에 덮였다")
    _check("c.toml" in str(args.config), f"config: {args.config}")


def test_verbose_from_either_position() -> None:
    parser = _build_parser()
    _check(parser.parse_args(["-v", "doctor"]).verbose, "앞쪽 -v 손실")
    _check(parser.parse_args(["doctor", "-v"]).verbose, "뒤쪽 -v 손실")
    _check(not parser.parse_args(["doctor"]).verbose, "-v 없이 참이 됨")


def test_version_declared_in_one_place() -> None:
    """버전 문자열은 crex/__init__.py 와 README 두 곳에만 있어야 한다.

    리포트에 찍힌 버전으로 "그때 뭘로 돌렸나"를 되짚는 것이 목적인데, 소스
    어딘가에 숫자를 또 적어 두면 그 값이 거짓이 될 수 있다. 사람이 맞추는 곳은
    README 하나뿐이고, 그것도 여기서 대조한다.
    """
    root = Path(__file__).resolve().parents[1]

    readme = (root / "README.md").read_text(encoding="utf-8")
    _check(f"버전 {__version__}" in readme, f"README 에 '버전 {__version__}' 이 없다")

    # 소스에서 __init__.py 말고 버전을 또 적은 곳이 있는가.
    literal = re.compile(r'"' + re.escape(__version__) + r'(\.\d+)*"')
    offenders = []
    for path in sorted((root / "crex").rglob("*.py")):
        if path.name == "__init__.py" and path.parent.name == "crex":
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "version" in line.lower() and literal.search(line):
                offenders.append(f"{path.relative_to(root)}:{number}")
    _check(not offenders, f"버전을 직접 적은 곳이 있다: {offenders}")

    # 실제로 파생되는지도 본다 — import 만 해 두고 안 쓰면 위 검사를 통과한다.
    _check(Context.__dataclass_fields__["version"].default == __version__,
           "관제 화면이 다른 버전을 알린다")
    _check(
        inspect.signature(to_sarif).parameters["version"].default == __version__,
        "SARIF 리포트가 다른 버전을 적는다",
    )


def test_workspace_command_parses() -> None:
    """확인·고정·해제 세 형태를 다 받아야 한다."""
    parser = _build_parser()

    show = parser.parse_args(["workspace"])
    _check(show.command == "workspace", f"command: {show.command}")
    _check(show.path is None and not show.clear, "인자 없는 형태가 깨졌다")

    setter = parser.parse_args(["workspace", "/tmp/x"])
    _check(str(setter.path) in ("/tmp/x", "\\tmp\\x"), f"path: {setter.path}")

    _check(parser.parse_args(["workspace", "--clear"]).clear, "--clear 손실")


def test_scan_paths_still_parse() -> None:
    args = _build_parser().parse_args(["scan", "a.cpp", "b.py", "--out", "reports"])
    _check(args.paths == ["a.cpp", "b.py"], f"paths: {args.paths}")


def _result_with_finding(severity: Severity) -> ReviewResult:
    return ReviewResult(kept=[Finding(
        path="src/buffer.cpp", line=8, dimension=Dimension.DEFECT,
        severity=severity, rule_id="cpp.dangling-after-realloc",
        message="resize 이후 포인터 무효화",
    )])


def test_markdown_survives_cp949_console() -> None:
    """한국어 Windows 콘솔은 cp949 다. 리포트에 이모지가 있어 그대로 두면 죽는다.

    대상 사용자가 정확히 그 환경이므로 반드시 터지는 경로다.
    """
    markdown = to_markdown(_result_with_finding(Severity.HIGH))
    _check("🔴" in markdown, "심각도 표시가 사라졌다 — 테스트 전제가 깨짐")

    raw = io.BytesIO()
    console = io.TextIOWrapper(raw, encoding="cp949")   # 한국어 Windows 기본
    original = sys.stdout
    sys.stdout = console
    try:
        force_utf8_output()
        print(markdown)
        console.flush()
    finally:
        sys.stdout = original

    decoded = raw.getvalue().decode("utf-8", errors="replace")
    _check("높음" in decoded, f"한글이 깨졌다:\n{decoded[:200]}")
    _check("cpp.dangling-after-realloc" in decoded, "본문이 유실됐다")


#: 존댓말 종결. 이것으로 끝나면 검사를 통과한다.
_POLITE_TAILS = ("니다", "십시오", "시오", "세요")

#: 종결어미처럼 보이지만 아닌 것들. 조사(`저장소마다`)와 연결어미(`번이라`,
#: `아니라`, `이에 따라`)가 같은 글자로 끝난다.
_NOT_ENDINGS = ("마다", "이라", "아니라", "따라", "보다", "대로", "하나", "가지")


def _plain_form_words(text: str) -> list[str]:
    """해라체로 끝나는 낱말을 찾는다. 없으면 빈 목록."""
    hits = []
    for word in re.findall(r"[가-힣]+", text):
        if len(word) < 2 or not word.endswith(("다", "라")):
            continue
        if word.endswith(_POLITE_TAILS) or word.endswith(_NOT_ENDINGS):
            continue
        hits.append(word)
    return hits


def _runtime_strings(path: Path):
    """런타임 문자열만 준다 — 주석·독스트링·프롬프트 상수는 뺀다.

    프롬프트는 **모델에게 주는 지시**라 사람에게 하는 말의 규칙을 따르지 않는다.
    이름 끝의 `_PROMPT`/`_SYSTEM`/`_USER` 가 그 표시다.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None:
                docstrings.add(doc)

    def is_prompt(node) -> bool:
        current = node
        for _ in range(8):
            current = parents.get(current)
            if current is None:
                return False
            if isinstance(current, ast.Assign):
                return any(
                    isinstance(t, ast.Name) and t.id.endswith(("_PROMPT", "_SYSTEM", "_USER"))
                    for t in current.targets
                )
        return False

    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if node.value in docstrings or not re.search(r"[가-힣]", node.value):
            continue
        if is_prompt(node):
            continue
        yield node.lineno, node.value


def test_user_facing_text_is_written_in_polite_form() -> None:
    """사용자가 읽는 한국어는 전부 합쇼체다 — CLI 도, 화면도 (`CLAUDE.md` 의 언어 표).

    코드 주석과 독스트링은 대상이 아니다. 개발자가 읽는 글이라 해라체가 맞고,
    그것까지 바꾸면 diff 만 커진다. LLM 프롬프트도 아니다 — 모델에게 주는 지시는
    말투가 아니라 지시문이고, 건드리면 리뷰 동작이 바뀔 수 있다.

    한 곳이라도 반말이 남으면 사용자는 그 한 줄만 유독 다르게 읽는다.
    """
    root = Path(__file__).resolve().parents[1]
    offenders = []
    checked = 0

    for path in sorted((root / "crex").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for line, value in _runtime_strings(path):
            checked += 1
            words = _plain_form_words(value)
            if words:
                rel = path.relative_to(root).as_posix()
                offenders.append(f"{rel}:{line} {words} — {' '.join(value.split())[:60]}")

    _check(checked > 100, f"검사한 문자열이 너무 적다({checked}건) — 수집이 깨졌다")

    # 화면 파일: JS 는 문자열 리터럴만, HTML 은 주석을 뺀 텍스트만 본다.
    web = root / "crex" / "viz" / "web"
    literal = re.compile(r"'((?:[^'\\\n]|\\.)*)'|\"((?:[^\"\\\n]|\\.)*)\"", re.S)
    for path in sorted(web.glob("*.js")):
        source = path.read_text(encoding="utf-8")
        for match in literal.finditer(source):
            value = next(g for g in match.groups() if g is not None)
            words = _plain_form_words(value)
            if words:
                line = source[: match.start()].count("\n") + 1
                offenders.append(f"web/{path.name}:{line} {words} — {value[:60]}")

    html = re.sub(r"<!--.*?-->", "", (web / "index.html").read_text(encoding="utf-8"), flags=re.S)
    for chunk in re.split(r"<[^>]*>", html):
        words = _plain_form_words(chunk)
        if words:
            offenders.append(f"web/index.html {words} — {' '.join(chunk.split())[:60]}")

    _check(not offenders, "사용자에게 반말을 합니다:\n" + "\n".join(offenders))


def test_powershell_scripts_carry_a_bom() -> None:
    """한글이 든 .ps1 은 UTF-8 BOM 으로 저장해야 한다.

    Windows PowerShell 5.1(폐쇄망 장비의 기본)은 BOM 이 없는 .ps1 을 시스템 ANSI
    코드페이지로 읽는다. 한국어 Windows 에서는 cp949 다. 주석의 한글이 깨지는 데서
    끝나지 않는 것이 문제다 — cp949 선행 바이트가 뒤따르는 따옴표나 괄호를 삼켜
    스크립트가 통째로 파싱 오류로 죽는다.

    실제로 `tools/package.ps1` 이 그 상태로 들어와 있었다(2026-08-29). 반입 번들을
    만드는 스크립트라, 못 돌리면 폐쇄망에 아무것도 못 넣는다.
    """
    root = Path(__file__).resolve().parents[1]
    offenders = []
    for path in sorted(root.rglob("*.ps1")):
        # 번들 산출물은 소스의 복사본이라 두 번 볼 이유가 없다.
        if any(part.startswith("dist") or part == ".git" for part in path.parts):
            continue
        data = path.read_bytes()
        # ASCII 만 있는 스크립트는 어느 코드페이지로 읽어도 같다.
        if all(byte < 128 for byte in data):
            continue
        if not data.startswith(b"\xef\xbb\xbf"):
            offenders.append(str(path.relative_to(root)))
    _check(not offenders, f"BOM 없는 한글 .ps1: {offenders}")


def test_exit_code_signals_high_severity() -> None:
    """CI 게이트로 쓰려면 종료 코드가 정확해야 한다."""

    class Args:
        out = None
        stem = "review"

    original = sys.stdout
    sys.stdout = io.StringIO()
    try:
        high = _emit(_result_with_finding(Severity.HIGH), Args())
        medium = _emit(_result_with_finding(Severity.MEDIUM), Args())
        empty = _emit(ReviewResult(), Args())
    finally:
        sys.stdout = original

    _check(high == 1, f"high 인데 종료 코드 {high}")
    _check(medium == 0, f"medium 인데 종료 코드 {medium}")
    _check(empty == 0, f"지적 없는데 종료 코드 {empty}")


TESTS = [
    test_global_flags_accepted_after_subcommand,
    test_subcommand_flag_wins_over_global,
    test_global_flag_not_clobbered_by_subparser_default,
    test_verbose_from_either_position,
    test_repo_is_accepted_as_alias,
    test_workspace_defaults_to_none,
    test_version_declared_in_one_place,
    test_workspace_command_parses,
    test_scan_paths_still_parse,
    test_markdown_survives_cp949_console,
    test_user_facing_text_is_written_in_polite_form,
    test_powershell_scripts_carry_a_bom,
    test_exit_code_signals_high_severity,
]


def main() -> int:
    # 한국어 Windows 콘솔은 cp949 다. 출력에 한글과 기호가 섞여 있어
    # 맞춰주지 않으면 테스트 러너 자체가 UnicodeEncodeError 로 죽는다.
    from crex.cli import force_utf8_output

    force_utf8_output()
    failures = 0
    for test in TESTS:
        try:
            test()
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {test.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"ERROR {test.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"ok   {test.__name__}")
    print(f"\n{len(TESTS) - failures}/{len(TESTS)} 통과")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
