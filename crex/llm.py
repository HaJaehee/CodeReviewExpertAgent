"""vLLM (OpenAI 호환) 클라이언트.

stdlib `urllib` 만 사용한다 — 폐쇄망에 반입할 wheel 을 하나라도 줄이기 위함이다.

설계상 두 가지가 핵심이다.

1. **Guided decoding** — 검증 단계의 출력은 JSON Schema 로 강제한다. 25~40B 급
   모델은 자유 형식으로 두면 스키마를 어긴다. vLLM 은 xgrammar 백엔드로 유효하지
   않은 토큰을 마스킹하므로 파싱 실패가 구조적으로 사라진다.
2. **토큰 예산 강제** — Qwen3.6/Gemma4 가 256K 컨텍스트를 지원해도 쓰지 않는다.
   컨텍스트를 늘릴수록 정밀도가 떨어진다는 것이 반복 확인된 결과다. 프롬프트
   빌더가 `max_input_tokens`(기본 8192)에서 잘라낸다.

## 구조화 출력은 실패할 수 있다고 전제한다

guided decoding 은 서버 구성에 따라 **있거나 없다**. vLLM 버전마다 전달 방식이
`response_format` 과 `guided_json` 으로 갈리고, xgrammar 백엔드는 `maxLength`
같은 일부 키워드를 컴파일하지 못해 400 을 돌려준다. 이 셋 중 하나만 어긋나도
모든 청크의 호출이 실패하고, 그 결과는 "지적 0건" 과 구분되지 않는다.

그래서 여기서 두 가지를 한다.

- **사다리(ladder)** — 전달 방식과 스키마 엄격도를 순서대로 낮춰가며 재시도하고,
  성공한 조합을 기억해 이후 호출에 바로 쓴다. 서버 버전 차이는 스스로 흡수한다.
- **명시적 실패** — 사다리를 다 내려가도 안 되면 `StructuredOutputError` 로
  무엇을 시도했고 서버가 뭐라 했는지 그대로 올린다. 조용히 빈 결과를 만들지 않는다.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

#: 문자 수 → 토큰 수 개산 계수. 코드는 산문보다 토큰 밀도가 높아 보수적으로 잡는다.
#: 정확한 토크나이저를 쓰지 않는 이유는 폐쇄망 반입 의존성을 늘리지 않기 위함이다.
CHARS_PER_TOKEN = 3.0

#: 구조화 출력 전달 방식. 앞의 것부터 시도한다.
STRUCTURED_MODES = ("response_format", "guided_json")

#: xgrammar 가 컴파일하지 못하는 JSON Schema 키워드. 스키마 거부가 나면 이것들을
#: 떼고 한 번 더 시도한다. 떼도 안전한 이유: 길이·개수 상한은 품질 보조 장치일 뿐,
#: 환각을 막는 것은 enum 이다. enum 은 절대 완화하지 않는다.
RELAXABLE_KEYWORDS = ("maxLength", "minLength", "maxItems", "minItems", "pattern")


class LLMError(RuntimeError):
    """복구 불가능한 LLM 호출 실패."""


class LLMHTTPError(LLMError):
    """HTTP 상태 코드를 동반한 실패. 사다리가 400 계열을 구분하는 데 쓴다."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body}")
        self.status = status
        self.body = body


class StructuredOutputError(LLMError):
    """guided decoding 을 어떤 방식으로도 성립시키지 못했다.

    이 예외가 뜨면 리뷰 결과가 0건인 것은 코드가 깨끗해서가 아니다. 호출부는
    이것을 삼키지 말고 사용자에게 그대로 보여야 한다.
    """


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
    #: 구조화 출력 전달 방식. "auto" 는 response_format → guided_json 순으로
    #: 시도하고 되는 것을 기억한다. 특정 방식을 고정하려면 이름을 직접 적는다.
    structured_output_mode: str = "auto"
    #: guided_json 경로에서 함께 보낼 백엔드 이름. 비우면 보내지 않는다.
    #: 최신 vLLM 은 이 필드를 모르는 키로 보고 400 을 돌려주므로 기본은 비워둔다.
    guided_decoding_backend: str = ""
    #: 추론 모드를 끄기 위한 추가 파라미터 (Qwen3.x 계열의 enable_thinking 등).
    extra_body: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.structured_output_mode not in STRUCTURED_MODES + ("auto",):
            raise ValueError(
                f"structured_output_mode = {self.structured_output_mode!r} 가 잘못되었다. "
                f"사용 가능: {['auto', *STRUCTURED_MODES]}"
            )

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


@dataclass
class ProbeStep:
    """`doctor` 가 출력하는 진단 한 줄."""

    label: str
    ok: bool
    detail: str


class LLMClient:
    """OpenAI 호환 chat completions 클라이언트."""

    def __init__(self, config: EndpointConfig) -> None:
        self.config = config
        #: 성공한 (전달방식, 스키마완화여부) 조합. 첫 호출에서 정해지고 이후 재사용된다.
        #: 청크마다 사다리를 다시 내려가면 400 왕복이 청크 수만큼 쌓인다.
        self._resolved: tuple[str, bool] | None = None
        #: 사다리를 끝까지 내려가고도 실패했다면 그 결과는 청크마다 같다.
        #: 기억해 두고 이후 호출은 즉시 같은 예외를 올린다 — 청크 수만큼
        #: 400 왕복을 반복하면 리뷰가 몇 분씩 헛돈다.
        self._structured_failed: StructuredOutputError | None = None
        self._lock = threading.Lock()

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

        if json_schema is None:
            return self._post_with_retry(payload)
        return self._complete_structured(payload, json_schema, schema_name)

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
        """엔드포인트 연결과 모델 존재를 확인한다.

        **이것만으로 리뷰가 된다고 판단하면 안 된다.** 여기서 보내는 요청에는
        스키마가 없어서, guided decoding 이 통째로 막혀 있어도 통과한다.
        실제 리뷰 경로를 확인하려면 `probe()` 를 쓴다.
        """
        try:
            reply = self.complete("You are a test.", "Reply with the single word: ok", max_output_tokens=8)
        except Exception as exc:  # noqa: BLE001 - 진단 목적이므로 모두 잡는다
            return False, f"{type(exc).__name__}: {exc}"
        return True, reply.strip()

    def probe(self, schemas: list[tuple[str, dict[str, Any]]]) -> list["ProbeStep"]:
        """리뷰가 실제로 쓰는 경로를 단계별로 확인한다.

        doctor 가 "OK" 라고 했는데 리뷰는 0건이던 사고가 이 메서드를 만든 이유다.
        연결만 보면 안 되고, **파이프라인이 실제로 보내는 스키마**를 그대로 보내
        서버가 받아들이는지, 받아들인 뒤 enum 을 지키는지까지 봐야 한다.
        """
        steps: list[ProbeStep] = []

        ok, detail = self.health()
        steps.append(ProbeStep("연결·모델", ok, detail))
        if not ok:
            for name, _ in schemas:
                steps.append(ProbeStep(f"구조화 출력 ({name})", False, "연결 실패로 건너뜀"))
            return steps

        for name, schema in schemas:
            steps.append(self._probe_schema(name, schema))
        return steps

    def _probe_schema(self, name: str, schema: dict[str, Any]) -> "ProbeStep":
        # 진단은 매번 사다리를 처음부터 탄다.
        self._resolved = None
        self._structured_failed = None
        try:
            raw = self.complete(
                "You are a JSON generator. Reply with JSON only.",
                "Produce one example object that satisfies the given schema.",
                json_schema=schema,
                schema_name=name,
                max_output_tokens=300,
            )
        except StructuredOutputError as exc:
            return ProbeStep(f"구조화 출력 ({name})", False, str(exc))
        except Exception as exc:  # noqa: BLE001 - 진단 목적이므로 모두 잡는다
            return ProbeStep(f"구조화 출력 ({name})", False, f"{type(exc).__name__}: {exc}")

        mode, relaxed = self._resolved or (self.config.structured_output_mode, False)
        note = f"{mode}{' (스키마 완화됨)' if relaxed else ''}"

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            if _extract_first_json_object(raw) is None:
                return ProbeStep(
                    f"구조화 출력 ({name})", False,
                    f"{note} — JSON 이 아닌 응답: {raw[:120]!r}",
                )
            return ProbeStep(
                f"구조화 출력 ({name})", False,
                f"{note} — 스키마가 강제되지 않는다 (설명이 섞여 나옴). "
                "vLLM 의 guided decoding 백엔드를 확인하라",
            )

        violations = _enum_violations(parsed, schema)
        if violations:
            return ProbeStep(
                f"구조화 출력 ({name})", False,
                f"{note} — 요청은 통과했으나 enum 이 지켜지지 않는다: {violations[0]}. "
                "이 상태에서는 라인 번호 환각을 막지 못한다",
            )
        return ProbeStep(f"구조화 출력 ({name})", True, f"{note} — enum 준수 확인")

    # -- internals ---------------------------------------------------------

    def _complete_structured(
        self, payload: dict[str, Any], schema: dict[str, Any], name: str
    ) -> str:
        """전달 방식과 스키마 엄격도를 낮춰가며 시도한다.

        서버가 스키마를 거부하는 이유는 대개 둘 중 하나다. 전달 방식이 그 버전과
        안 맞거나(response_format vs guided_json), 백엔드가 컴파일하지 못하는
        키워드가 섞여 있거나. 둘 다 요청을 바꿔보면 알 수 있는 것이라 사람이
        설정을 고칠 때까지 기다리지 않고 여기서 흡수한다.
        """
        if self._structured_failed is not None:
            raise self._structured_failed

        attempted: list[str] = []
        last: LLMError | None = None

        for mode, relax in self._ladder():
            candidate = _relax_schema(schema) if relax else schema
            body = dict(payload)
            _apply_structured_output(body, candidate, name, mode, self.config.guided_decoding_backend)
            label = f"{mode}{'+완화' if relax else ''}"
            try:
                raw = self._post_with_retry(body)
            except LLMHTTPError as exc:
                if exc.status not in (400, 404, 422):
                    raise
                attempted.append(f"{label} → HTTP {exc.status}")
                last = exc
                log.debug("구조화 출력 %s 거부됨: %s", label, exc)
                continue

            self._remember(mode, relax, label)
            return raw

        failure = StructuredOutputError(
            "guided decoding 을 성립시키지 못했다 — 이 엔드포인트로는 리뷰가 불가능하다.\n"
            f"  엔드포인트: {self.config.model} @ {self.config.base_url}\n"
            f"  시도: {', '.join(attempted) or '(없음)'}\n"
            f"  마지막 응답: {last}\n"
            "  vLLM 이 --guided-decoding-backend 와 함께 떠 있는지, "
            "llm.*.structured_output_mode 설정이 서버 버전과 맞는지 확인하라."
        )
        with self._lock:
            self._structured_failed = failure
        raise failure

    def _ladder(self) -> list[tuple[str, bool]]:
        """시도할 (전달방식, 스키마완화) 조합을 순서대로 돌려준다."""
        resolved = self._resolved
        if resolved is not None:
            return [resolved]

        configured = self.config.structured_output_mode
        modes = STRUCTURED_MODES if configured == "auto" else (configured,)
        # 엄격한 스키마를 모든 방식에서 먼저 시도한다. 완화는 마지막 수단이다 —
        # maxLength 가 빠지면 모델이 장광설을 늘어놓을 수 있어 품질이 조금 나빠진다.
        return [(m, False) for m in modes] + [(m, True) for m in modes]

    def _remember(self, mode: str, relax: bool, label: str) -> None:
        """성공한 조합을 고정한다. 첫 확정 때만 로그를 남긴다."""
        with self._lock:
            if self._resolved is not None:
                return
            self._resolved = (mode, relax)

        log.info(
            "구조화 출력 방식 확정: %s (%s @ %s)", label, self.config.model, self.config.base_url
        )
        if relax:
            log.warning(
                "스키마의 길이·개수 제약을 떼어야 통과했다. enum 은 유지되므로 라인·룰 "
                "환각 차단은 그대로지만, 서버의 guided decoding 백엔드를 점검하라."
            )

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
                last_error = LLMHTTPError(exc.code, body)
                if exc.code in (400, 404, 422):
                    # 스키마/모델명 오류는 재시도해도 동일하다. 즉시 중단해
                    # 호출부(사다리)가 다른 조합을 시도할 수 있게 한다.
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


def _apply_structured_output(
    payload: dict[str, Any],
    schema: dict[str, Any],
    name: str,
    mode: str,
    backend: str = "",
) -> None:
    """전달 방식에 맞춰 스키마를 요청 본문에 싣는다."""
    if mode == "guided_json":
        # 구버전 vLLM 경로.
        payload["guided_json"] = schema
        if backend:
            # 최신 vLLM 은 이 키를 모른다고 400 을 낸다. 설정에 적었을 때만 보낸다.
            payload["guided_decoding_backend"] = backend
        return
    payload["response_format"] = {
        "type": "json_schema",
        "json_schema": {"name": name, "schema": schema, "strict": True},
    }


def _relax_schema(schema: Any) -> Any:
    """백엔드가 컴파일하지 못하는 키워드를 재귀적으로 걷어낸다.

    **enum 과 type 은 건드리지 않는다.** 이 프로젝트에서 환각을 막는 것은 그 둘이고,
    길이·개수 상한은 프롬프트 위생에 가깝다. 완화의 대가를 여기로 한정한다.
    """
    if isinstance(schema, dict):
        return {
            key: _relax_schema(value)
            for key, value in schema.items()
            if key not in RELAXABLE_KEYWORDS
        }
    if isinstance(schema, list):
        return [_relax_schema(item) for item in schema]
    return schema


def _enum_violations(value: Any, schema: Any, path: str = "$") -> list[str]:
    """응답이 스키마의 enum 을 실제로 지켰는지 확인한다.

    요청이 200 으로 돌아왔다는 것과 guided decoding 이 켜져 있다는 것은 다른
    이야기다. 서버가 `response_format` 을 조용히 무시하는 구성이 실제로 있다.
    """
    if not isinstance(schema, dict):
        return []

    allowed = schema.get("enum")
    if allowed is not None and value not in allowed:
        preview = allowed[:5]
        more = f" 외 {len(allowed) - 5}개" if len(allowed) > 5 else ""
        return [f"{path}={value!r} 은 허용값 {preview}{more} 에 없다"]

    found: list[str] = []
    if isinstance(value, dict):
        for key, sub in (schema.get("properties") or {}).items():
            if key in value:
                found.extend(_enum_violations(value[key], sub, f"{path}.{key}"))
    elif isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(value):
                found.extend(_enum_violations(item, items, f"{path}[{index}]"))
    return found
