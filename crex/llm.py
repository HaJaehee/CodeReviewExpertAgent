"""vLLM (OpenAI 호환) 클라이언트.

stdlib `urllib` 만 사용한다 — 폐쇄망에 반입할 wheel 을 하나라도 줄이기 위함이다.

설계상 두 가지가 핵심이다.

1. **Guided decoding** — 검증 단계의 출력은 JSON Schema 로 강제한다. 25~40B 급
   모델은 자유 형식으로 두면 스키마를 어긴다. vLLM 은 xgrammar 백엔드로 유효하지
   않은 토큰을 마스킹하므로 파싱 실패가 구조적으로 사라진다.
2. **토큰 예산 강제** — Qwen3.6/Gemma4 가 256K 컨텍스트를 지원해도 쓰지 않는다.
   컨텍스트를 늘릴수록 정밀도가 떨어진다는 것이 반복 확인된 결과다. 프롬프트
   빌더가 `max_input_tokens`(기본 8192)에서 잘라낸다.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

#: 문자 수 → 토큰 수 개산 계수. 코드는 산문보다 토큰 밀도가 높아 보수적으로 잡는다.
#: 정확한 토크나이저를 쓰지 않는 이유는 폐쇄망 반입 의존성을 늘리지 않기 위함이다.
CHARS_PER_TOKEN = 3.0


class LLMError(RuntimeError):
    """복구 불가능한 LLM 호출 실패."""


@dataclass
class EndpointConfig:
    """vLLM 인스턴스 하나에 대한 설정.

    생성(RuleChecker)과 검증(ReviewFilter)은 서로 다른 모델을 쓰는 것이 좋다.
    같은 모델의 자기검증보다 교차 모델 검증이 환각을 더 잘 잡는다.
    """

    base_url: str
    model: str
    api_key: str = "EMPTY"  # vLLM 은 보통 인증이 없지만 헤더는 보내야 한다
    temperature: float = 0.0
    max_output_tokens: int = 1024
    max_input_tokens: int = 8192
    timeout: float = 120.0
    max_retries: int = 3
    #: 구조화 출력 전달 방식. 최신 vLLM 은 "response_format", 구버전은 "guided_json".
    structured_output_mode: str = "response_format"
    #: 추론 모드를 끄기 위한 추가 파라미터 (Qwen3.x 계열의 enable_thinking 등).
    extra_body: dict[str, Any] = field(default_factory=dict)

    @property
    def chat_url(self) -> str:
        return self.base_url.rstrip("/") + "/chat/completions"


def estimate_tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN) + 1


def truncate_to_budget(text: str, max_tokens: int, marker: str = "\n... (생략) ...\n") -> str:
    """토큰 예산에 맞춰 텍스트 *중간부*를 잘라낸다.

    코드 청크는 앞(시그니처)과 뒤(반환·정리 구문)가 모두 중요하므로 꼬리를
    자르는 대신 가운데를 버린다.
    """
    budget_chars = int(max_tokens * CHARS_PER_TOKEN)
    if len(text) <= budget_chars:
        return text
    keep = (budget_chars - len(marker)) // 2
    if keep <= 0:
        return text[:budget_chars]
    return text[:keep] + marker + text[-keep:]


class LLMClient:
    """OpenAI 호환 chat completions 클라이언트."""

    def __init__(self, config: EndpointConfig) -> None:
        self.config = config

    # -- public ------------------------------------------------------------

    def complete(
        self,
        system: str,
        user: str,
        *,
        json_schema: dict[str, Any] | None = None,
        schema_name: str = "response",
        max_output_tokens: int | None = None,
    ) -> str:
        """1회 호출 후 assistant 메시지 본문을 그대로 돌려준다."""
        budget = self.config.max_input_tokens - estimate_tokens(system) - 256
        user = truncate_to_budget(user, max(budget, 512))

        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.config.temperature,
            "max_tokens": max_output_tokens or self.config.max_output_tokens,
            "stream": False,
        }
        payload.update(self.config.extra_body)

        if json_schema is not None:
            self._apply_structured_output(payload, json_schema, schema_name)

        return self._post_with_retry(payload)

    def complete_json(
        self,
        system: str,
        user: str,
        json_schema: dict[str, Any],
        *,
        schema_name: str = "response",
        max_output_tokens: int | None = None,
    ) -> dict[str, Any]:
        """스키마를 강제해 호출하고 파싱된 dict 를 돌려준다.

        guided decoding 이 켜져 있으면 파싱은 사실상 실패하지 않는다. 그럼에도
        구버전 vLLM 이나 미지원 백엔드를 대비해 관대한 추출을 한 번 시도한다.
        """
        raw = self.complete(
            system,
            user,
            json_schema=json_schema,
            schema_name=schema_name,
            max_output_tokens=max_output_tokens,
        )
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            extracted = _extract_first_json_object(raw)
            if extracted is None:
                raise LLMError(f"JSON 파싱 실패, guided decoding 설정을 확인하라: {raw[:400]!r}")
            return extracted

    def health(self) -> tuple[bool, str]:
        """엔드포인트 연결과 모델 존재를 확인한다. 폐쇄망 반입 직후 점검용."""
        try:
            reply = self.complete("You are a test.", "Reply with the single word: ok", max_output_tokens=8)
        except Exception as exc:  # noqa: BLE001 - 진단 목적이므로 모두 잡는다
            return False, f"{type(exc).__name__}: {exc}"
        return True, reply.strip()

    # -- internals ---------------------------------------------------------

    def _apply_structured_output(
        self, payload: dict[str, Any], schema: dict[str, Any], name: str
    ) -> None:
        mode = self.config.structured_output_mode
        if mode == "guided_json":
            # 구버전 vLLM 경로.
            payload["guided_json"] = schema
            payload["guided_decoding_backend"] = "xgrammar"
        else:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": name, "schema": schema, "strict": True},
            }

    def _post_with_retry(self, payload: dict[str, Any]) -> str:
        last_error: Exception | None = None
        for attempt in range(self.config.max_retries):
            if attempt:
                # 폐쇄망은 대개 GPU 1~2대를 공유하므로 429/503 이 흔하다. 선형 백오프면 충분하다.
                time.sleep(1.5 * attempt)
            try:
                return self._post(payload)
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")[:500]
                last_error = LLMError(f"HTTP {exc.code}: {body}")
                if exc.code in (400, 404, 422):
                    # 스키마/모델명 오류는 재시도해도 동일하다. 즉시 중단해 로그를 명확히 남긴다.
                    raise last_error from exc
                log.warning("LLM 호출 실패 (attempt %d/%d): %s", attempt + 1, self.config.max_retries, last_error)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = LLMError(f"연결 실패: {exc}")
                log.warning("LLM 연결 실패 (attempt %d/%d): %s", attempt + 1, self.config.max_retries, exc)

        raise last_error or LLMError("알 수 없는 실패")

    def _post(self, payload: dict[str, Any]) -> str:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.config.chat_url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.config.api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.config.timeout) as response:
            body = json.loads(response.read().decode("utf-8"))

        choices = body.get("choices") or []
        if not choices:
            raise LLMError(f"빈 응답: {body}")
        message = choices[0].get("message", {})
        content = message.get("content")
        if content is None:
            raise LLMError(f"content 없음 (reasoning 전용 응답?): {message}")
        return content


def _extract_first_json_object(text: str) -> dict[str, Any] | None:
    """텍스트에서 첫 번째 균형 잡힌 JSON 객체를 꺼낸다.

    guided decoding 이 꺼진 환경에서 모델이 ```json 펜스나 설명을 덧붙이는 경우에
    대한 방어막이다.
    """
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : index + 1])
                except json.JSONDecodeError:
                    return None
    return None
