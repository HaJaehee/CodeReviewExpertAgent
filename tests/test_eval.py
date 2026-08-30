"""평가 하네스 지표 계산 검증.

KBI/FAR 계산이 틀리면 이후 모든 Phase 판단이 틀어진다. 손으로 검산 가능한
작은 케이스로 고정한다.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))

from crex.schema import Dimension, Finding, FilterVerdict, RejectReason, ReviewResult, Severity  # noqa: E402

# 반입 번들에는 `eval/` 이 없다 — 골든셋 튜닝은 이 저장소를 고치는 쪽 일이라
# `tools/package.ps1` 이 빼낸다. 저장소에서는 전부 돌고, 번들에서는 통째로
# 건너뛴다. 없는 것을 조용히 통과시키는 게 아니라 건너뛴다고 말한다.
try:
    from run_eval import GoldenCase, GoldenDefect, Report, score_case  # noqa: E402
except ImportError:  # pragma: no cover - 번들에서만 탄다
    GoldenCase = GoldenDefect = Report = score_case = None


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _finding(path: str, line: int, rule_id: str = "cpp.x") -> Finding:
    return Finding(
        path=path, line=line, dimension=Dimension.DEFECT,
        severity=Severity.HIGH, rule_id=rule_id, message="m",
    )


def _result(kept: list[Finding], rejected: int = 0) -> ReviewResult:
    result = ReviewResult(kept=kept)
    for _ in range(rejected):
        result.rejected.append(
            FilterVerdict(_finding("x", 1), False, "r", RejectReason.VERDICT_NO)
        )
    return result


def test_exact_match_counts_as_hit() -> None:
    case = GoldenCase("c1", ".", "d.diff", [GoldenDefect("a.cpp", 10)])
    score = score_case(case, _result([_finding("a.cpp", 10)]))
    _check(score.matched == 1, f"매칭: {score.matched}")
    _check(score.false_alarms == 0, f"오탐: {score.false_alarms}")


def test_line_tolerance() -> None:
    """리뷰어는 결함 라인을 정확히 짚지 않는다. ±3 까지 매칭한다."""
    case = GoldenCase("c1", ".", "d.diff", [GoldenDefect("a.cpp", 10)])

    _check(score_case(case, _result([_finding("a.cpp", 13)])).matched == 1, "13 은 매칭되어야 한다")
    _check(score_case(case, _result([_finding("a.cpp", 7)])).matched == 1, "7 은 매칭되어야 한다")

    far_off = score_case(case, _result([_finding("a.cpp", 14)]))
    _check(far_off.matched == 0, "14 는 매칭되면 안 된다")
    _check(far_off.false_alarms == 1, f"오탐으로 세야 한다: {far_off.false_alarms}")


def test_different_file_is_false_alarm() -> None:
    case = GoldenCase("c1", ".", "d.diff", [GoldenDefect("a.cpp", 10)])
    score = score_case(case, _result([_finding("b.cpp", 10)]))
    _check(score.matched == 0, "다른 파일이 매칭됨")
    _check(score.false_alarms == 1, f"오탐: {score.false_alarms}")


def test_duplicate_findings_do_not_inflate_recall() -> None:
    """같은 결함을 두 번 지적해도 재현율은 1건만 오른다."""
    case = GoldenCase("c1", ".", "d.diff", [GoldenDefect("a.cpp", 10)])
    score = score_case(case, _result([_finding("a.cpp", 10), _finding("a.cpp", 11)]))
    _check(score.matched == 1, f"중복이 재현율을 부풀림: {score.matched}")
    # 둘 다 골든 결함 근처이므로 오탐은 아니다.
    _check(score.false_alarms == 0, f"오탐: {score.false_alarms}")


def test_missed_defect_lowers_kbi() -> None:
    case = GoldenCase("c1", ".", "d.diff", [GoldenDefect("a.cpp", 10), GoldenDefect("a.cpp", 50)])
    score = score_case(case, _result([_finding("a.cpp", 10)]))
    _check(score.matched == 1 and score.total_defects == 2, f"{score}")


def test_report_aggregates() -> None:
    report = Report()
    # 케이스 1: 결함 2건 중 1건 탐지, 지적 2건 중 1건 오탐
    case1 = GoldenCase("c1", ".", "d", [GoldenDefect("a.cpp", 10), GoldenDefect("a.cpp", 50)])
    report.scores.append(score_case(case1, _result([_finding("a.cpp", 10), _finding("a.cpp", 99)])))
    # 케이스 2: 결함 2건 중 2건 탐지, 오탐 없음
    case2 = GoldenCase("c2", ".", "d", [GoldenDefect("b.cpp", 5), GoldenDefect("b.cpp", 20)])
    report.scores.append(score_case(case2, _result([_finding("b.cpp", 5), _finding("b.cpp", 20)])))

    # KBI = 3 탐지 / 4 결함
    _check(abs(report.kbi - 0.75) < 1e-9, f"KBI: {report.kbi}")
    # FAR = 1 오탐 / 4 지적
    _check(abs(report.far - 0.25) < 1e-9, f"FAR: {report.far}")

    payload = report.to_dict()
    _check(payload["summary"]["total_defects"] == 4, f"{payload['summary']}")
    _check(payload["summary"]["total_matched"] == 3, f"{payload['summary']}")
    _check("KBI" in report.render(), "렌더 출력에 지표가 없음")


def test_empty_golden_is_safe() -> None:
    """지적도 결함도 없으면 0으로 나눠 터지면 안 된다."""
    report = Report()
    report.scores.append(score_case(GoldenCase("c", ".", "d", []), _result([])))
    _check(report.kbi == 0.0 and report.far == 0.0, "0 분모 처리 실패")
    report.render()  # 예외 없이 렌더되어야 한다


TESTS = [
    test_exact_match_counts_as_hit,
    test_line_tolerance,
    test_different_file_is_false_alarm,
    test_duplicate_findings_do_not_inflate_recall,
    test_missed_defect_lowers_kbi,
    test_report_aggregates,
    test_empty_golden_is_safe,
]


def main() -> int:
    # 한국어 Windows 콘솔은 cp949 다. 출력에 한글과 기호가 섞여 있어
    # 맞춰주지 않으면 테스트 러너 자체가 UnicodeEncodeError 로 죽는다.
    from crex.cli import force_utf8_output

    force_utf8_output()
    if score_case is None:
        print("     (eval/ 없음 — 반입 번들에서는 건너뜁니다)")
        return 0

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
