"""vLLM 을 흉내내는 최소 HTTP 서버.

stdlib 만으로 `/v1/chat/completions` 를 구현해, LLM 없이도 파이프라인 전 구간을
실제 HTTP 경로로 태울 수 있게 한다. 요청의 `response_format.json_schema.name`
으로 생성 호출과 검증 호출을 구분한다.

guided decoding 을 흉내내지는 않는다 — 대신 **스키마를 실제로 검사해서**
enum 위반이 있으면 테스트가 실패하게 한다. 스키마 제약이 의도대로 구성되는지를
검증하는 것이 목적이다.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable


class FakeVLLM:
    """컨텍스트 매니저로 쓰는 가짜 vLLM."""

    def __init__(
        self,
        *,
        on_findings: Callable[[dict], dict] | None = None,
        on_verdict: Callable[[dict], dict] | None = None,
    ) -> None:
        self.on_findings = on_findings or (lambda _schema: {"findings": []})
        self.on_verdict = on_verdict or (
            lambda _schema: {"verdict": "yes", "code_present": True, "reason": "확인됨"}
        )
        self.requests: list[dict[str, Any]] = []
        self.schema_violations: list[str] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        assert self._server is not None, "서버가 시작되지 않았다"
        host, port = self._server.server_address[:2]
        return f"http://127.0.0.1:{port}/v1"

    def __enter__(self) -> "FakeVLLM":
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # 테스트 출력을 더럽히지 않는다
                pass

            def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler 규약
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                outer.requests.append(payload)

                schema, name = _extract_schema(payload)
                content = (
                    outer.on_verdict(schema) if name == "verdict" else outer.on_findings(schema)
                )
                outer._validate(content, schema, name)

                body = json.dumps(
                    {
                        "id": "fake",
                        "object": "chat.completion",
                        "choices": [
                            {
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": json.dumps(content, ensure_ascii=False),
                                },
                                "finish_reason": "stop",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ).encode("utf-8")

                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        if self._thread:
            self._thread.join(timeout=5)

    # -- 스키마 검사 --------------------------------------------------------

    def _validate(self, content: dict, schema: dict | None, name: str) -> None:
        """응답이 요청된 스키마의 enum 제약을 지키는지 확인한다.

        진짜 guided decoding 이라면 위반이 불가능하다. 여기서 위반이 잡힌다는 건
        스키마가 의도대로 구성되지 않았다는 뜻이므로 테스트가 알아야 한다.
        """
        if not schema:
            return
        if name == "findings":
            items = (schema.get("properties", {}).get("findings", {})).get("items", {})
            props = items.get("properties", {})
            allowed_lines = props.get("line", {}).get("enum")
            allowed_rules = props.get("rule_id", {}).get("enum")
            for finding in content.get("findings", []):
                if allowed_lines is not None and finding.get("line") not in allowed_lines:
                    self.schema_violations.append(
                        f"line {finding.get('line')} 이 enum {allowed_lines} 밖"
                    )
                if allowed_rules is not None and finding.get("rule_id") not in allowed_rules:
                    self.schema_violations.append(
                        f"rule_id {finding.get('rule_id')!r} 이 enum 밖"
                    )


def _extract_schema(payload: dict) -> tuple[dict | None, str]:
    response_format = payload.get("response_format") or {}
    if response_format.get("type") == "json_schema":
        spec = response_format.get("json_schema", {})
        return spec.get("schema"), spec.get("name", "")
    if "guided_json" in payload:
        return payload["guided_json"], ""
    return None, ""
