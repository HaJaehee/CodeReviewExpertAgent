"""ReviewFilter 검증.

vLLM 없이 동작하도록 LLMClient 를 스텁으로 대체한다. 결정론적 단계는 LLM 을
전혀 부르지 않아야 하므로, 스텁 호출 횟수를 세는 것 자체가 검증이 된다.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crex.chunk import Chunker, parse_unified_diff  # noqa: E402
from crex.filter import ReviewFilter  # noqa: E402
from crex.schema import Dimension, Finding, RejectReason, Severity  # noqa: E402
from tests.test_chunk import CPP_DIFF, CPP_SOURCE  # noqa: E402


class StubClient:
    """지정된 판정을 순서대로 돌려주는 가짜 LLM 클라이언트."""

    def __init__(self, responses: list[dict]) -> None:
        self.responses = list(responses)
        self.calls: list[str] = []

    def complete_json(self, system, user, schema, *, schema_name="response", max_output_tokens=None):
        self.calls.append(user)
        if not self.responses:
            raise AssertionError("스텁에 준비된 응답보다 많은 호출이 발생했다")
        return self.responses.pop(0)


class ExplodingClient:
    """항상 실패하는 클라이언트. 보수적 기각 경로를 검증한다."""

    def __init__(self) -> None:
        self.calls = 0

    def complete_json(self, *args, **kwargs):
        self.calls += 1
        raise RuntimeError("연결 거부")


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _build_chunks() -> dict:
    fd = parse_unified_diff(CPP_DIFF)[0]
    chunks = Chunker().chunk_file(fd, CPP_SOURCE)
    return {c.chunk_id: c for c in chunks}


def _finding(line: int, rule_id: str = "cpp.dangling-pointer", path: str = "src/buffer.cpp", **kwargs) -> Finding:
    return Finding(
        path=path,
        line=line,
        dimension=Dimension.DEFECT,
        severity=Severity.HIGH,
        rule_id=rule_id,
        message="resize() 이후 raw 포인터가 무효화된다",
        **kwargs,
    )


def test_out_of_range_line_rejected_without_llm() -> None:
    """청크 범위 밖 라인은 LLM 을 부르지 않고 즉시 기각되어야 한다."""
    chunks = _build_chunks()
    client = StubClient([])
    rf = ReviewFilter(client, chunks)

    kept, rejected = rf.filter([_finding(9999)])

    _check(kept == [], f"범위 밖 지적이 유지됨: {kept}")
    _check(len(rejected) == 1, f"기각 1건을 기대했으나 {len(rejected)}건")
    _check(
        rejected[0].reject_reason is RejectReason.LINE_OUT_OF_RANGE,
        f"기각 사유 불일치: {rejected[0].reject_reason}",
    )
    _check(rejected[0].short_circuited, "결정론적 기각인데 short_circuited 가 False")
    _check(client.calls == [], f"LLM 이 호출됨: {len(client.calls)}회")


def test_unchanged_line_rejected_in_diff_mode() -> None:
    """변경되지 않은 라인 지적은 diff 리뷰 범위 밖이므로 기각한다."""
    chunks = _build_chunks()
    client = StubClient([])
    # 라인 14 는 청크에 포함되지만 unchanged 다.
    kept, rejected = ReviewFilter(client, chunks).filter([_finding(14)])

    _check(kept == [], f"미변경 라인 지적이 유지됨: {kept}")
    _check(
        rejected[0].reject_reason is RejectReason.LINE_NOT_CHANGED,
        f"기각 사유 불일치: {rejected[0].reject_reason}",
    )
    _check(client.calls == [], "LLM 이 호출됨")


def test_unchanged_line_allowed_in_scan_mode() -> None:
    """전체 파일 감사 모드에서는 미변경 라인도 리뷰 대상이다."""
    chunks = _build_chunks()
    client = StubClient([{"verdict": "yes", "code_present": True, "reason": "확인됨"}])
    kept, _ = ReviewFilter(client, chunks, require_changed_line=False).filter([_finding(14)])

    _check(len(kept) == 1, f"scan 모드에서 유지되지 않음: {kept}")
    _check(len(client.calls) == 1, f"LLM 호출 1회를 기대했으나 {len(client.calls)}회")


def test_llm_verdict_no_rejects() -> None:
    chunks = _build_chunks()
    client = StubClient([{"verdict": "no", "code_present": True, "reason": "코드에 그런 문제가 없다"}])
    kept, rejected = ReviewFilter(client, chunks).filter([_finding(16)])

    _check(kept == [], f"기각되어야 할 지적이 유지됨: {kept}")
    _check(rejected[0].reject_reason is RejectReason.VERDICT_NO, f"{rejected[0].reject_reason}")
    _check("없다" in rejected[0].reason, f"판정 근거가 전달되지 않음: {rejected[0].reason}")


def test_code_not_present_rejects_even_if_verdict_yes() -> None:
    """code_present=False 는 verdict 보다 강하다 — 명백한 환각 신호이기 때문."""
    chunks = _build_chunks()
    client = StubClient([{"verdict": "yes", "code_present": False, "reason": "해당 코드 없음"}])
    kept, rejected = ReviewFilter(client, chunks).filter([_finding(16)])

    _check(kept == [], f"환각 지적이 유지됨: {kept}")
    _check(rejected[0].reject_reason is RejectReason.CODE_NOT_FOUND, f"{rejected[0].reject_reason}")


def test_valid_finding_kept() -> None:
    chunks = _build_chunks()
    client = StubClient([{"verdict": "yes", "code_present": True, "reason": "raw 포인터 무효화 확인"}])
    kept, rejected = ReviewFilter(client, chunks).filter([_finding(17)])

    _check(len(kept) == 1, f"유효한 지적이 기각됨: {rejected}")
    _check(kept[0].line == 17, f"라인 불일치: {kept[0].line}")
    _check(kept[0].verifier_comment == "raw 포인터 무효화 확인", f"검증 코멘트 누락: {kept[0].verifier_comment}")


def test_duplicate_rejected() -> None:
    chunks = _build_chunks()
    client = StubClient([{"verdict": "yes", "code_present": True, "reason": "ok"}])
    kept, rejected = ReviewFilter(client, chunks).filter([_finding(16), _finding(16)])

    _check(len(kept) == 1, f"중복 제거 실패: {len(kept)}건 유지")
    _check(len(rejected) == 1 and rejected[0].reject_reason is RejectReason.DUPLICATE,
           f"중복 기각 실패: {rejected}")


def test_llm_error_rejects_conservatively() -> None:
    """검증 호출이 실패하면 통과시키지 않고 기각한다."""
    chunks = _build_chunks()
    client = ExplodingClient()
    rf = ReviewFilter(client, chunks)
    kept, rejected = rf.filter([_finding(16)])

    _check(kept == [], "검증 실패 시 지적이 통과됨 — 보수적 기각이 아님")
    _check(rejected[0].reject_reason is RejectReason.FILTER_ERROR, f"{rejected[0].reject_reason}")
    _check(rf.stats.errors == 1, f"오류 카운트 불일치: {rf.stats.errors}")


def test_stats_reject_rate() -> None:
    chunks = _build_chunks()
    client = StubClient([
        {"verdict": "yes", "code_present": True, "reason": "ok"},
        {"verdict": "no", "code_present": True, "reason": "근거 없음"},
    ])
    rf = ReviewFilter(client, chunks)
    rf.filter([_finding(15), _finding(16, rule_id="cpp.other"), _finding(9999, rule_id="cpp.x")])

    _check(rf.stats.total == 3, f"총계 불일치: {rf.stats.total}")
    _check(rf.stats.kept == 1, f"유지 건수 불일치: {rf.stats.kept}")
    _check(rf.stats.rejected_deterministic == 1, f"결정론적 기각 불일치: {rf.stats.rejected_deterministic}")
    _check(rf.stats.rejected_llm == 1, f"LLM 기각 불일치: {rf.stats.rejected_llm}")
    _check(abs(rf.stats.reject_rate - 2 / 3) < 1e-9, f"기각률 불일치: {rf.stats.reject_rate}")


def test_strip_thinking_tags_and_reasoning_recovery() -> None:
    """<think> 태그나 내부 코드가 있더라도 순수 JSON 을 복구해야 한다."""
    from crex.llm import _extract_first_json_object, _strip_thinking_tags

    raw_with_think = (
        "<think>\nHere is some reasoning with code { int a = 1; }\n</think>\n"
        '{"verdict": "yes", "code_present": "yes", "reason": "ok"}'
    )
    cleaned = _strip_thinking_tags(raw_with_think)
    _check("<think>" not in cleaned, "think 태그가 남아있음")
    extracted = _extract_first_json_object(cleaned)
    _check(extracted is not None and extracted.get("verdict") == "yes", f"JSON 추출 실패: {extracted}")

    # 미완료 think 태그
    raw_unclosed = "<think>\nThinking about { foo }... unfinished"
    cleaned_unclosed = _strip_thinking_tags(raw_unclosed)
    _check(cleaned_unclosed == "", f"미완료 think 태그 정리 실패: {cleaned_unclosed}")


def test_review_filter_accepts_reasoning_only_response() -> None:
    """검증 모델이 content=None 이고 reasoning 으로만 응답해도 ReviewFilter 가 정상 승인한다."""
    from crex.llm import EndpointConfig, LLMClient, _extract_best_json_object, _extract_reasoning_text, _repair_truncated_json
    import json

    class FakeReasoningClient(LLMClient):
        def __init__(self) -> None:
            super().__init__(EndpointConfig(base_url="http://fake", model="fake-thinker"))

        def _post_with_retry(self, payload):
            message = {
                "role": "assistant",
                "content": None,
                "reasoning": (
                    "Let me examine the code snippet. "
                    "realloc 이후 반환값을 대입하지 않고 이전 포인터를 참조하는 것은 심각한 결함입니다. "
                    "지적이 타당하므로 승인합니다. Verdict: yes. Code is present."
                ),
            }
            body = {"choices": [{"index": 0, "message": message, "finish_reason": "stop"}]}
            choices = body.get("choices") or []
            self._call_state.finish_reason = choices[0].get("finish_reason")
            msg = choices[0].get("message", {})
            content = msg.get("content")
            if content is None or (isinstance(content, str) and not content.strip()):
                reasoning = _extract_reasoning_text(msg, choices[0])
                if reasoning:
                    extracted = _extract_best_json_object(reasoning) or _repair_truncated_json(reasoning)
                    if extracted is not None:
                        return json.dumps(extracted, ensure_ascii=False)
                    return reasoning
            return content

    chunks = _build_chunks()
    client = FakeReasoningClient()
    rf = ReviewFilter(client, chunks)
    kept, rejected = rf.filter([_finding(15)])

    _check(len(kept) == 1, f"reasoning-only 응답이어도 지적이 승인되어야 한다: {kept}")
    _check(kept[0].verifier_comment is not None, "verifier_comment 가 채워져야 한다")
    _check("realloc" in kept[0].verifier_comment, f"코멘트에 추론 내용 보존 확인: {kept[0].verifier_comment}")
    _check(rf.stats.errors == 0, f"FILTER_ERROR 가 발생하지 않아야 한다: {rf.stats.errors}")


def test_excluded_rule_rejected_deterministically_without_llm() -> None:
    """exclude_rules 에 매칭되는 룰 지적은 LLM 을 부르지 않고 즉시 기각되어야 한다."""
    chunks = _build_chunks()
    client = StubClient([])
    rf = ReviewFilter(client, chunks, exclude_rules=["cpp.dangling-*"])

    # 라인 15 는 changed_lines 에 포함됨
    kept, rejected = rf.filter([_finding(15, rule_id="cpp.dangling-pointer")])

    _check(kept == [], f"제외된 룰 지적이 유지됨: {kept}")
    _check(len(rejected) == 1, f"기각 1건을 기대했으나 {len(rejected)}건")
    _check(
        rejected[0].reject_reason is RejectReason.RULE_EXCLUDED,
        f"기각 사유 불일치: {rejected[0].reject_reason}",
    )
    _check(rejected[0].short_circuited, "결정론적 기각인데 short_circuited 가 False")
    _check(client.calls == [], f"LLM 이 호출됨: {len(client.calls)}회")


def test_path_scoped_rule_exclusion() -> None:
    """특정 파일 경로에 한정된 룰 제외 패턴이 해당 파일에만 적용된다."""
    chunks = _build_chunks()
    client = StubClient([{"verdict": "yes", "code_present": True, "reason": "유효함"}])
    rf = ReviewFilter(client, chunks, exclude_rules=["src/buffer.cpp:cpp.dangling-*"])

    # 1) src/buffer.cpp 지적 -> 룰 제외로 즉시 기각 (LLM 안 부름)
    kept, rejected = rf.filter([_finding(15, rule_id="cpp.dangling-pointer", path="src/buffer.cpp")])
    _check(kept == [], "제외 경로의 룰 지적이 유지됨")
    _check(len(rejected) == 1, "기각되어야 함")
    _check(rejected[0].reject_reason is RejectReason.RULE_EXCLUDED, f"기각 사유: {rejected[0].reject_reason}")
    _check(client.calls == [], "LLM 이 호출됨")

    # 2) 다른 룰(any.hardcoded-secret) -> 제외되지 않고 LLM 호출되어 유지됨
    kept2, rejected2 = rf.filter([_finding(15, rule_id="any.hardcoded-secret", path="src/buffer.cpp")])
    _check(len(kept2) == 1, f"다른 룰은 통과되어야 함: {kept2}")
    _check(len(rejected2) == 0, "기각되지 않아야 함")
    _check(len(client.calls) == 1, "LLM 이 1회 호출되어야 함")


TESTS = [
    test_out_of_range_line_rejected_without_llm,
    test_unchanged_line_rejected_in_diff_mode,
    test_unchanged_line_allowed_in_scan_mode,
    test_llm_verdict_no_rejects,
    test_code_not_present_rejects_even_if_verdict_yes,
    test_valid_finding_kept,
    test_duplicate_rejected,
    test_llm_error_rejects_conservatively,
    test_stats_reject_rate,
    test_strip_thinking_tags_and_reasoning_recovery,
    test_review_filter_accepts_reasoning_only_response,
    test_excluded_rule_rejected_deterministically_without_llm,
    test_path_scoped_rule_exclusion,
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
