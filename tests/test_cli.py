"""CLI 인자 처리와 출력 인코딩 검증.

둘 다 개발 중 실제로 물렸던 것들이라 회귀 방지가 목적이다.
"""

from __future__ import annotations

import io
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crx.cli import _build_parser, _emit, force_utf8_output  # noqa: E402
from crx.report import to_markdown  # noqa: E402
from crx.schema import Dimension, Finding, ReviewResult, Severity  # noqa: E402


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def test_global_flags_accepted_after_subcommand() -> None:
    """`crx review --staged --repo X` 가 동작해야 한다.

    argparse 기본 동작은 전역 옵션을 서브커맨드 *앞*에만 허용한다. 사람은
    자연스럽게 뒤에 쓰고, 문서에도 그렇게 적힌 예시가 있었다.
    """
    parser = _build_parser()

    after = parser.parse_args(["review", "--staged", "--repo", "/tmp/x", "--config", "/tmp/c.toml"])
    _check(str(after.repo) in ("/tmp/x", "\\tmp\\x"), f"repo: {after.repo}")
    _check(after.config is not None and "c.toml" in str(after.config), f"config: {after.config}")
    _check(after.staged, "staged 플래그 손실")

    before = parser.parse_args(["--repo", "/tmp/x", "review", "--staged"])
    _check(str(before.repo) in ("/tmp/x", "\\tmp\\x"), f"repo: {before.repo}")


def test_subcommand_flag_wins_over_global() -> None:
    """양쪽에 주면 서브커맨드 쪽(나중에 쓴 것)이 이겨야 한다."""
    args = _build_parser().parse_args(["--repo", "/tmp/a", "review", "--repo", "/tmp/b"])
    _check(str(args.repo).endswith("b"), f"repo: {args.repo}")


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
    test_scan_paths_still_parse,
    test_markdown_survives_cp949_console,
    test_exit_code_signals_high_severity,
]


def main() -> int:
    # 한국어 Windows 콘솔은 cp949 다. 출력에 한글과 기호가 섞여 있어
    # 맞춰주지 않으면 테스트 러너 자체가 UnicodeEncodeError 로 죽는다.
    from crx.cli import force_utf8_output

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
