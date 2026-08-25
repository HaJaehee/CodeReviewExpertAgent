"""구조화 출력 사다리와 진단 회귀 테스트.

이 파일이 존재하는 이유는 실제 사고다. 폐쇄망 장비에서 `doctor` 는 전부 OK 인데
C++/C# 프로젝트를 무엇을 넣어도 지적이 0건이었다. 원인은 코드가 아니라 **보고**에
있었다 — guided decoding 요청이 400 으로 거절되고 있었고, 그 실패는 청크마다
`except Exception` 에 삼켜져 로그 한 줄로만 남았다. 리포트는 "지적 사항 없음",
종료 코드는 0. 고장이 깨끗한 코드와 똑같이 보였다.

그래서 두 가지를 고정한다.

1. **서버 성향 차이는 사다리가 흡수한다** — response_format / guided_json,
   엄격 / 완화 스키마 조합을 순서대로 시도한다.
2. **흡수하지 못한 실패는 반드시 드러난다** — `ReviewResult.errors` 에 실리고,
   `healthy` 가 False 가 되고, 리포트 맨 위에 경고가 붙는다.
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crex.filter import VERDICT_SCHEMA  # noqa: E402
from crex.generate import build_findings_schema  # noqa: E402
from crex.llm import (  # noqa: E402
    RELAXABLE_KEYWORDS,
    EndpointConfig,
    LLMClient,
    StructuredOutputError,
    _relax_schema,
)
from crex.report import to_markdown  # noqa: E402
from crex.schema import ReviewResult  # noqa: E402

FINDINGS_SCHEMA = build_findings_schema(
    rule_ids=["cpp.use-after-move", "cpp.dangling-after-realloc"],
    allowed_lines=[41, 42],
    max_findings=2,
)


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _has_relaxable(obj) -> bool:
    if isinstance(obj, dict):
        return any(k in RELAXABLE_KEYWORDS for k in obj) or any(
            _has_relaxable(v) for v in obj.values()
        )
    if isinstance(obj, list):
        return any(_has_relaxable(v) for v in obj)
    return False


class _Server:
    """성향(flavor)에 따라 구조화 출력 요청을 다르게 다루는 가짜 vLLM.

    폐쇄망에서 마주칠 수 있는 서버 구성을 흉내낸다. 각 성향은 실제로 보고된
    vLLM 동작에 대응한다.
    """

    def __init__(self, flavor: str) -> None:
        self.flavor = flavor
        self.seen: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler 규약
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                outer.seen.append(payload)

                schema = None
                if "response_format" in payload:
                    if outer.flavor == "old_vllm":
                        return self._error("response_format is not supported")
                    schema = payload["response_format"]["json_schema"]["schema"]
                elif "guided_json" in payload:
                    if outer.flavor == "new_vllm":
                        return self._error("guided_json was removed")
                    if "guided_decoding_backend" in payload:
                        return self._error("unknown field: guided_decoding_backend")
                    schema = payload["guided_json"]

                if schema is None:
                    return self._ok("ok")  # 스키마 없는 health 요청
                if outer.flavor == "rejects_all":
                    return self._error("guided decoding unavailable")
                if outer.flavor == "xgrammar_strict" and _has_relaxable(schema):
                    return self._error("xgrammar cannot compile maxLength/maxItems")
                if outer.flavor == "ignores_schema":
                    return self._ok('다음과 같습니다:\n```json\n{"findings": []}\n```')
                if outer.flavor == "violates_enum":
                    return self._ok(json.dumps({"verdict": "maybe", "code_present": True,
                                                "reason": "x"}))
                return self._ok(_sample_for(schema))

            def _ok(self, content: str):
                body = json.dumps(
                    {"choices": [{"index": 0, "message": {"role": "assistant",
                                                          "content": content},
                                  "finish_reason": "stop"}]},
                    ensure_ascii=False,
                ).encode("utf-8")
                self._send(200, body)

            def _error(self, message: str):
                self._send(400, json.dumps({"error": {"message": message}}).encode("utf-8"))

            def _send(self, code: int, body: bytes):
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)

    def __enter__(self) -> "_Server":
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()

    @property
    def client(self) -> LLMClient:
        host, port = self._server.server_address[:2]
        return LLMClient(
            EndpointConfig(base_url=f"http://127.0.0.1:{port}/v1", model="fake",
                           max_retries=1, timeout=10.0)
        )


def _sample_for(schema: dict) -> str:
    """스키마의 enum 첫 값을 그대로 쓴다 — guided decoding 이 하는 일과 같다."""
    props = schema.get("properties", {})
    if "findings" in props:
        item = props["findings"]["items"]["properties"]
        return json.dumps(
            {"findings": [{"line": item["line"]["enum"][0],
                           "rule_id": item["rule_id"]["enum"][0],
                           "severity": "high", "message": "검증용", "suggestion": ""}]},
            ensure_ascii=False,
        )
    return json.dumps({"verdict": "yes", "code_present": True, "reason": "확인"},
                      ensure_ascii=False)


# -- 사다리 -----------------------------------------------------------------


def test_modern_server_uses_response_format() -> None:
    with _Server("modern") as server:
        client = server.client
        client.complete_json("s", "u", FINDINGS_SCHEMA, schema_name="findings")
        _check(client._resolved == ("response_format", False), f"{client._resolved}")


def test_old_vllm_falls_back_to_guided_json() -> None:
    """구버전 vLLM 에서도 리뷰가 돌아야 한다. 예전에는 여기서 조용히 0건이 됐다."""
    with _Server("old_vllm") as server:
        client = server.client
        out = client.complete_json("s", "u", FINDINGS_SCHEMA, schema_name="findings")
        _check(client._resolved == ("guided_json", False), f"{client._resolved}")
        _check(len(out["findings"]) == 1, f"{out}")


def test_new_vllm_keeps_response_format() -> None:
    with _Server("new_vllm") as server:
        client = server.client
        client.complete_json("s", "u", FINDINGS_SCHEMA, schema_name="findings")
        _check(client._resolved == ("response_format", False), f"{client._resolved}")


def test_strict_backend_gets_relaxed_schema() -> None:
    """xgrammar 가 maxLength 를 못 컴파일해도 enum 은 살려서 통과시킨다."""
    with _Server("xgrammar_strict") as server:
        client = server.client
        client.complete_json("s", "u", FINDINGS_SCHEMA, schema_name="findings")
        _check(client._resolved is not None and client._resolved[1] is True,
               f"완화 스키마로 성공했어야 한다: {client._resolved}")


def test_guided_decoding_backend_is_not_sent_by_default() -> None:
    """최신 vLLM 은 이 필드를 모르는 키로 보고 400 을 낸다. 적었을 때만 보낸다."""
    with _Server("old_vllm") as server:
        client = server.client
        client.complete_json("s", "u", FINDINGS_SCHEMA, schema_name="findings")
        guided = [p for p in server.seen if "guided_json" in p]
        _check(guided and all("guided_decoding_backend" not in p for p in guided),
               "기본 설정에서 guided_decoding_backend 를 보내면 안 된다")


def test_ladder_is_resolved_once_not_per_call() -> None:
    """청크마다 400 왕복을 반복하면 리뷰가 몇 분씩 헛돈다."""
    with _Server("old_vllm") as server:
        client = server.client
        for _ in range(3):
            client.complete_json("s", "u", FINDINGS_SCHEMA, schema_name="findings")
        rejected = [p for p in server.seen if "response_format" in p]
        _check(len(rejected) == 1, f"거절당한 방식을 재시도했다: {len(rejected)}회")


def test_total_failure_raises_instead_of_returning_empty() -> None:
    """사다리를 다 내려가도 안 되면 조용히 빈 결과를 만들지 않는다."""
    with _Server("rejects_all") as server:
        try:
            server.client.complete_json("s", "u", FINDINGS_SCHEMA, schema_name="findings")
        except StructuredOutputError as exc:
            _check("response_format" in str(exc) and "guided_json" in str(exc),
                   f"무엇을 시도했는지 메시지에 남아야 한다: {exc}")
            return
        raise AssertionError("StructuredOutputError 가 나야 한다")


def test_relax_keeps_enum_and_type() -> None:
    """완화는 길이·개수 제약만 뗀다. enum 을 떼면 환각 차단이 무너진다."""
    relaxed = _relax_schema(FINDINGS_SCHEMA)
    item = relaxed["properties"]["findings"]["items"]["properties"]
    _check(item["line"]["enum"] == [41, 42], f"{item['line']}")
    _check(len(item["rule_id"]["enum"]) == 2, f"{item['rule_id']}")
    _check(not _has_relaxable(relaxed), "완화 후에도 제약이 남아 있다")
    _check(_has_relaxable(FINDINGS_SCHEMA), "원본 스키마는 그대로여야 한다 (부작용 금지)")


# -- 진단 -------------------------------------------------------------------


def test_probe_passes_on_healthy_server() -> None:
    with _Server("modern") as server:
        steps = server.client.probe([("findings", FINDINGS_SCHEMA), ("verdict", VERDICT_SCHEMA)])
        _check(all(s.ok for s in steps), f"{[(s.label, s.detail) for s in steps if not s.ok]}")


def test_probe_catches_what_health_alone_misses() -> None:
    """이것이 사고의 핵심이다. 연결은 멀쩡한데 구조화 출력만 막힌 상태."""
    with _Server("rejects_all") as server:
        client = server.client
        healthy, _ = client.health()
        _check(healthy, "health() 는 통과한다 — 그래서 doctor 가 OK 라고 했다")

        steps = client.probe([("findings", FINDINGS_SCHEMA)])
        _check(steps[0].ok, "연결 단계는 통과해야 한다")
        _check(not steps[1].ok, "구조화 출력 단계가 실패로 보고돼야 한다")


def test_probe_detects_unenforced_schema() -> None:
    """200 을 준다고 guided decoding 이 켜진 것은 아니다."""
    with _Server("ignores_schema") as server:
        steps = server.client.probe([("findings", FINDINGS_SCHEMA)])
        _check(not steps[1].ok, "스키마가 강제되지 않는 것을 잡아야 한다")


def test_probe_detects_enum_violation() -> None:
    with _Server("violates_enum") as server:
        steps = server.client.probe([("verdict", VERDICT_SCHEMA)])
        _check(not steps[1].ok, "enum 위반을 잡아야 한다")
        _check("enum" in steps[1].detail, f"{steps[1].detail}")


# -- 보고 -------------------------------------------------------------------


def test_zero_findings_with_errors_is_not_reported_as_clean() -> None:
    """0건이 두 가지를 뜻하는 것을 리포트가 구분해야 한다."""
    broken = ReviewResult(chunks_reviewed=3, generation_errors=3,
                          errors=["a 생성 실패: HTTP 400"] * 3)
    _check(not broken.healthy, "실패가 있으면 healthy 가 아니다")
    markdown = to_markdown(broken)
    _check("지적 사항 없음" not in markdown, "고장을 '지적 사항 없음'으로 보고하면 안 된다")
    _check("신뢰할 수 없다" in markdown, f"경고가 없다:\n{markdown}")
    _check("doctor" in markdown, "다음에 뭘 할지 알려줘야 한다")

    clean = ReviewResult(chunks_reviewed=3)
    _check(clean.healthy, "오류가 없으면 healthy 다")
    _check("지적 사항 없음" in to_markdown(clean), "정상 0건은 그대로 보고한다")


def test_verdict_schema_property_order_survives_relaxation() -> None:
    """불변식: verdict 가 맨 앞이어야 Conclusion-First 가 성립한다."""
    _check(list(VERDICT_SCHEMA["properties"])[0] == "verdict", "원본 순서가 깨졌다")
    _check(list(_relax_schema(VERDICT_SCHEMA)["properties"])[0] == "verdict",
           "완화가 프로퍼티 순서를 바꿨다 — 생성 순서가 바뀌어 정확도가 나빠진다")


TESTS = [
    test_modern_server_uses_response_format,
    test_old_vllm_falls_back_to_guided_json,
    test_new_vllm_keeps_response_format,
    test_strict_backend_gets_relaxed_schema,
    test_guided_decoding_backend_is_not_sent_by_default,
    test_ladder_is_resolved_once_not_per_call,
    test_total_failure_raises_instead_of_returning_empty,
    test_relax_keeps_enum_and_type,
    test_probe_passes_on_healthy_server,
    test_probe_catches_what_health_alone_misses,
    test_probe_detects_unenforced_schema,
    test_probe_detects_enum_violation,
    test_zero_findings_with_errors_is_not_reported_as_clean,
    test_verdict_schema_property_order_survives_relaxation,
]


def main() -> int:
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
