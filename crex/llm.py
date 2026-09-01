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

## 응답이 잘리는 것은 스키마 문제가 아니다

guided decoding 이 완벽히 켜져 있어도 JSON 은 깨질 수 있다. `max_tokens` 에
걸리면 서버는 문법과 무관하게 그 자리에서 생성을 끊는다 — 지적을 세 건 쓰고
네 번째의 `suggestion` 에서 소스코드를 인용하던 중이었다면 응답은 문자열 한복판에서
끝난다. 청크마다 코드 길이가 다르니 이것은 **어떤 청크는 되고 어떤 청크는 안 되는**
형태로 나타나고, 예전에는 "guided decoding 설정을 확인하십시오" 라는 엉뚱한
메시지와 함께 그 청크의 지적이 통째로 사라졌다.

그래서 잘린 응답은 두 가지로 다룬다.

- **복구** — 마지막으로 온전히 끝난 값까지 잘라내고 열린 컨테이너를 닫아 되살린다.
  완성된 지적 세 건은 네 번째가 잘렸다는 이유로 버릴 것이 아니다.
- **보고** — 복구했든 못 했든 `TruncatedOutputError`/경고로 **잘렸다는 사실과
  올릴 설정 이름**을 말한다. 스키마를 의심하게 만들지 않는다.
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


class TruncatedOutputError(LLMError):
    """`max_tokens` 에 걸려 응답이 잘렸고, 남은 조각에서 건질 것이 없었다.

    스키마 실패와 구분되는 별도 타입인 이유는 처방이 다르기 때문이다. 이쪽의
    답은 `max_output_tokens` 를 올리거나 청크당 지적 수를 줄이는 것이지,
    guided decoding 설정을 들여다보는 것이 아니다.
    """


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
    max_output_tokens: int = 1600
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
                f"structured_output_mode = {self.structured_output_mode!r} 가 잘못되었습니다. "
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
        #: 직전 호출의 부수 정보(finish_reason, 잘림 복구 여부). 청크들이 워커
        #: 스레드에서 같은 클라이언트를 공유하므로 스레드 로컬이어야 한다.
        self._call_state = threading.local()

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

        guided decoding 이 켜져 있으면 문법 오류로 인한 파싱 실패는 사실상
        없다. 남는 실패 경로는 둘뿐이라 그 둘만 다룬다.

        1. 스키마가 강제되지 않아 설명·펜스가 섞여 나온 경우 → 관대한 추출
        2. `max_tokens` 에 걸려 응답이 잘린 경우 → 온전한 부분까지 복구

        2번을 복구하면서 `last_call_truncated` 를 세워 둔다. 호출부가 "이
        청크는 지적 일부를 잃었다" 를 사용자에게 말할 수 있어야 하기 때문이다.
        """
        self._call_state.salvaged = False
        budget = max_output_tokens or self.config.max_output_tokens

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
            pass

        extracted = _extract_first_json_object(raw)
        if extracted is not None:
            return extracted

        cut_off = self.last_finish_reason == "length"
        salvaged = _repair_truncated_json(raw)
        if salvaged is not None:
            self._call_state.salvaged = True
            log.warning(
                "응답이 %s 잘려 온전한 부분까지만 복구했습니다 (max_tokens=%d, %s). "
                "일부 항목이 빠졌을 수 있습니다.",
                "출력 토큰 한도에서" if cut_off else "중간에",
                budget,
                self.config.model,
            )
            return salvaged

        if cut_off:
            raise TruncatedOutputError(
                f"응답이 출력 토큰 한도({budget})에 걸려 잘렸고, 건질 수 있는 항목이 "
                "없었습니다. 설정의 max_output_tokens 를 올리거나 "
                "review.max_findings_per_chunk 를 줄이십시오. "
                f"끝부분: {raw[-200:]!r}"
            )
        raise LLMError(f"JSON 파싱 실패, guided decoding 설정을 확인하십시오: {raw[:400]!r}")

    @property
    def last_finish_reason(self) -> str | None:
        """직전 호출에서 서버가 알려준 종료 사유. `"length"` 면 잘린 것이다."""
        return getattr(self._call_state, "finish_reason", None)

    @property
    def last_call_truncated(self) -> bool:
        """직전 `complete_json` 이 잘린 응답을 복구해서 돌려줬는가.

        참이면 결과는 유효하지만 **완전하지 않다**. 조용히 넘기면 "지적 3건" 이
        "원래 3건" 과 구분되지 않는다.
        """
        return bool(getattr(self._call_state, "salvaged", False))

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
                # 진단 응답이 잘려 판정이 흐려지지 않을 만큼은 준다.
                max_output_tokens=600,
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
            parsed = None

        if parsed is None and self.last_finish_reason == "length":
            # 진단은 출력 예산을 짧게 잡으므로 여기서 잘리는 일이 있다. 이것을
            # "스키마 미적용"으로 보고하면 멀쩡한 서버를 뜯게 만든다. 앞부분만
            # 살려서 enum 검사는 그대로 한다 — 그게 이 단계의 목적이다.
            parsed = _repair_truncated_json(raw)
            if parsed is None:
                return ProbeStep(
                    f"구조화 출력 ({name})", False,
                    f"{note} — 응답이 진단용 출력 상한에서 잘려 enum 준수를 확인하지 "
                    "못했습니다. 스키마 요청 자체는 받아들여졌으므로 리뷰 경로의 "
                    "고장은 아닙니다",
                )
            note += " (응답이 잘려 앞부분만 검사)"

        if parsed is None:
            if _extract_first_json_object(raw) is None:
                return ProbeStep(
                    f"구조화 출력 ({name})", False,
                    f"{note} — JSON 이 아닌 응답: {raw[:120]!r}",
                )
            return ProbeStep(
                f"구조화 출력 ({name})", False,
                f"{note} — 스키마가 강제되지 않습니다 (설명이 섞여 나옴). "
                "vLLM 의 guided decoding 백엔드를 확인하십시오",
            )

        violations = _enum_violations(parsed, schema)
        if violations:
            return ProbeStep(
                f"구조화 출력 ({name})", False,
                f"{note} — 요청은 통과했으나 enum 이 지켜지지 않습니다: {violations[0]}. "
                "이 상태에서는 라인 번호 환각을 막지 못합니다",
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
            "guided decoding 을 성립시키지 못했습니다 — 이 엔드포인트로는 리뷰가 불가능합니다.\n"
            f"  엔드포인트: {self.config.model} @ {self.config.base_url}\n"
            f"  시도: {', '.join(attempted) or '(없음)'}\n"
            f"  마지막 응답: {last}\n"
            "  vLLM 이 --guided-decoding-backend 와 함께 떠 있는지, "
            "llm.*.structured_output_mode 설정이 서버 버전과 맞는지 확인하십시오."
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
                "스키마의 길이·개수 제약을 떼어야 통과했습니다. enum 은 유지되므로 라인·룰 "
                "환각 차단은 그대로지만, 서버의 guided decoding 백엔드를 점검하십시오."
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
        # 잘림 진단의 유일한 근거다. 본문 검사보다 먼저 남긴다 — content 가 비어
        # 예외로 나가는 경로에서도 "왜" 는 남아 있어야 한다.
        self._call_state.finish_reason = choices[0].get("finish_reason")
        message = choices[0].get("message", {})
        content = message.get("content")
        if content is None:
            raise LLMError(f"content 없음 (reasoning 전용 응답?): {message}")
        return content


def _extract_first_json_object(text: str) -> dict[str, Any] | None:
    """텍스트에서 파싱 가능한 첫 JSON 객체를 꺼낸다.

    guided decoding 이 꺼진 환경에서 모델이 ```json 펜스나 설명을 덧붙이는 경우에
    대한 방어막이다.

    **첫 `{` 에서 실패해도 포기하지 않는다.** 코드 리뷰 응답은 앞머리에 소스코드를
    인용하는 일이 잦고, `struct S { int a; }` 같은 조각이 먼저 걸리면 그것 하나
    때문에 뒤에 멀쩡히 붙어 있는 JSON 을 통째로 버리게 된다. 그래서 짝이 맞는
    객체가 JSON 이 아니면 **그 객체 뒤로 건너뛰어** 다음 것을 본다.

    반대로 짝이 맞지 않으면 거기서 멈춘다. 그 경우는 응답이 잘린 것이고, 계속
    파고들면 `{"findings": [{...` 안쪽의 지적 하나를 응답 전체로 착각해
    돌려주게 된다 — 나머지 지적이 조용히 사라지는 가장 나쁜 결말이다.
    잘린 응답은 `_repair_truncated_json()` 이 맡는다.
    """
    start = text.find("{")
    while start >= 0:
        end = _balanced_end(text, start)
        if end is None:
            return None
        try:
            parsed = json.loads(text[start:end])
        except json.JSONDecodeError:
            pass
        else:
            if isinstance(parsed, dict):
                return parsed
        start = text.find("{", end)
    return None


def _balanced_end(text: str, start: int) -> int | None:
    """`text[start]` 의 `{` 와 짝이 맞는 `}` 바로 다음 인덱스. 없으면 None."""
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
                return index + 1
    return None


def _repair_truncated_json(text: str) -> dict[str, Any] | None:
    """잘린 JSON 에서 **온전히 끝난 부분까지만** 살려낸다.

    `max_tokens` 에 걸린 응답은 문자열 한복판에서 끝난다. 흔한 모양은 이렇다.

        {"findings": [{...}, {...}, {"line": 42, "suggestion": "auto v = f('

    앞의 두 건은 완성돼 있고 세 번째만 미완이다. 이때 남은 두 건을 버리는 것은
    잘못된 손실이다 — 리뷰어는 지적이 왜 사라졌는지 알 방법이 없다.

    복구 규칙은 하나뿐이다. **값 하나가 확실히 끝난 지점까지만 취하고 열린
    컨테이너를 닫는다.** 그런 지점은 쉼표 앞과 닫는 괄호 뒤 두 곳이다. 내용을
    지어내거나 따옴표를 임의로 닫지 않는다 — 잘린 문자열을 닫아 버리면 반쪽짜리
    문장이 완성된 지적처럼 보이고, 그것이야말로 이 프로젝트가 막으려는 것이다.
    미완의 마지막 항목은 필수 필드가 빠진 채로 나오므로 호출부가 걸러낸다.
    """
    start = text.find("{")
    if start < 0:
        return None

    stack: list[str] = []
    in_string = False
    escaped = False
    #: (자를 위치, 그 지점에 열려 있던 컨테이너). 뒤에서부터 되짚는다.
    boundaries: list[tuple[int, list[str]]] = []

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
        elif char in "{[":
            stack.append(char)
        elif char in "}]":
            if not stack:
                break
            stack.pop()
            if not stack:
                # 여기서 객체가 닫혔다. 그런데도 이 함수까지 왔다는 것은 그
                # 객체가 파싱되지 않았다는 뜻이므로, 더 앞쪽 경계로 물러난다.
                break
            boundaries.append((index + 1, list(stack)))
        elif char == "," and stack:
            boundaries.append((index, list(stack)))

    for cut, open_containers in reversed(boundaries):
        candidate = text[start:cut] + "".join(
            "}" if opener == "{" else "]" for opener in reversed(open_containers)
        )
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
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
        return [f"{path}={value!r} 은 허용값 {preview}{more} 에 없습니다"]

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
