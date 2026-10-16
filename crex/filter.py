"""ReviewFilter — 2단계 파이프라인의 검증 관문.

BitsAI-CR 이 프로덕션에서 확인한 구조를 따른다. 생성 모델이 내놓은 지적을
그대로 내보내지 않고, 각 건을 독립적으로 재판정해 근거 없는 것을 걷어낸다.
ByteDance 실측으로 이 단계가 지적의 55.25% 를 기각하고 정밀도 77% 를 만들었다.

검증은 두 겹이다.

1. **결정론적 검사** — 지적한 라인이 청크 범위 안에 있는가, 실제로 변경된
   라인인가. 여기서 걸리는 건 LLM 을 부를 것도 없이 즉시 기각한다. 라인 번호
   환각의 대부분이 여기서 죽는다. 공짜이고 100% 확실하다.
2. **LLM 재판정** — 살아남은 건에 대해 *다른 모델*로 Yes/No 를 받는다. 같은
   모델의 자기검증보다 교차 모델 검증이 환각을 잘 잡는다.

LLM 재판정은 **Conclusion-First** 패턴을 쓴다. 결론 토큰을 먼저 뽑고 근거를
뒤에 붙이는 방식으로, BitsAI-CR 이 Reasoning-First 와 비교한 뒤 프로덕션에
채택한 형태다(정밀도 77.09%, 샘플당 1.7초). JSON Schema 의 프로퍼티 순서가
guided decoding 의 생성 순서를 결정하므로 `verdict` 를 맨 앞에 둔다.
"""

from __future__ import annotations

import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from .llm import LLMClient, estimate_tokens
from .rules import is_rule_excluded
from .schema import Finding, FilterVerdict, RejectReason, ReviewChunk

log = logging.getLogger(__name__)


#: 검증 응답 스키마. **프로퍼티 순서가 곧 생성 순서다** — verdict 를 맨 앞에 두어
#: Conclusion-First 를 강제한다. 순서를 바꾸면 지연시간과 정확도가 함께 나빠진다.
VERDICT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["yes", "no"],
            "description": "yes = 유효한 지적, no = 기각",
        },
        "code_present": {
            "type": "boolean",
            "description": "지적이 묘사하는 코드가 제시된 스니펫에 실제로 존재하는가",
        },
        "reason": {
            "type": "string",
            "maxLength": 500,
            "description": "지적의 타당성과 잠재 위험, 개선 방향에 대한 구체적인 검증 코멘트 (코드 인용 시 작은따옴표(') 또는 백틱(`) 사용, 큰따옴표 직접 사용 금지)",
        },
    },
    "required": ["verdict", "code_present", "reason"],
    "additionalProperties": False,
}


VERIFIER_SYSTEM = """\
당신은 코드리뷰 지적을 면밀히 검토하고 전문적 피드백을 제공하는 수석 리뷰 검증관이다.

제시된 코드 스니펫과 지적 내용을 바탕으로, 지적의 타당성을 평가하고 적극적인 검증 코멘트를 작성하라.
스니펫의 각 줄에는 `[added @142]` 형태로 변경 상태와 실제 라인 번호가 붙어 있다.
코드 스니펫 외에 `## 연관 심볼 및 피호출 함수 정의 (창 외부 문맥)` 섹션이 함께 제공될 수 있다.

## 승인 판정 기준 (적극적 승인)
다음 경우 적극적으로 "yes" 로 승인하라:
- 지적된 문제(잠재적 버그, 로직 결함, 리소스 누수/무효화, 예외 누락, 보안 취약점 등)가 코드의 흐름상 실제 위험이나 개선 필요성으로 확인되는 경우
- 피호출 함수 정의나 클래스 멤버 필드 문맥(창 외부 문맥)과 대조했을 때, 호출부/피호출부 간 계약 불일치(널/오류 반환 미처리, 예외 전파, 락/자원 누수 등)가 실제로 성립하는 경우
- 정적분석 도구의 분석이나 룰 기준에 부합하는 합리적인 지적인 경우
- 사소한 표현 차이가 있더라도 지적의 핵심 취지가 코드의 잠재적 위험을 정확히 짚고 있는 경우

## 기각 판정 기준 (명백한 오류만 기각)
다음 경우에만 "no" 로 기각하라:
- 지적이 묘사하는 코드/변수/함수가 스니펫에 전혀 존재하지 않는다 (명백한 환각)
- 지적의 주장이 실제 코드 동작이나 첨부된 피호출 함수 정의와 완전히 모순된다 (예: 널 체크가 명확히 있거나 피호출 함수가 절대 널을 반환하지 않는데 역참조 위험을 주장하는 경우)
- 실제 동작에 전혀 영향이 없는 극단적인 개인 취향 문제다

## 검증 코멘트 작성 지침
승인("yes")이든 기각("no")이든, 판정 근거(reason)는 단순 판정에 그치지 않고 **적극적이고 구체적인 검증 코멘트**로 한국어 1~3문장으로 작성하라.
- 승인 시: 이 지적이 왜 타당하며 실제 런타임에서 어떤 위험(크래시, 메모리 오염, 논리 오류 등)을 예방하는지, 제안된 해결책이 적절한지 설명하라.
- 기각 시: 왜 이 지적이 환각이거나 부당한지 명확한 이유를 제시하라.

## 소스코드 인용 시 따옴표 규칙 (필수 안전장치)
- reason 내에서 소스코드나 문자열 리터럴, 지적 내용을 인용할 때는 **반드시 작은따옴표(')나 백틱(`)을 사용**하거나, 큰따옴표를 이스케이프(\")하라.
- JSON 문자열 값 내부에서 이스케이프되지 않은 큰따옴표(")를 직접 쓰면 JSON 문법이 깨지므로 절대 금지한다.
  - 예: `reason": "코드 'printf(\"%s\", msg)' 에서 널 검사 누락 확인"` 또는 백틱(`) 사용

## 추론 및 응답 지침 (생각 과정 조절)
- 코드와 지적의 타당성을 면밀히 생각(추론)하되, 지나치게 길거나 장황한 생각 과정에 빠지지 마라. 핵심 근거를 간결히 짚은 후 신속하게 결론에 도달하여 최종 JSON 객체로 답변하라.
"""


VERIFIER_USER = """\
## 코드 스니펫
파일: {path}
{symbol_line}
```{lang}
{code}
```
{related_block}
## 검증할 지적
- 위치: {path}:{line}
- 분류: {dimension} / {severity}
- 룰: {rule_id}
- 내용: {message}
{suggestion_block}

## 지시
1. 이 지적이 타당한지 판정하고, 적극적인 검증 코멘트(reason)를 작성하라.
2. **코드 인용 따옴표**: reason 내에서 소스코드나 문자열을 인용할 때는 반드시 작은따옴표(') 또는 백틱(`)을 사용하여 JSON 이 깨지지 않도록 하라.
3. **간결한 추론**: 판단에 필요한 핵심적인 생각만 간결히 수행하고, 너무 긴 생각에 빠지지 말고 신속히 최종 JSON 객체로 답변하라.
"""




@dataclass
class FilterStats:
    total: int = 0
    kept: int = 0
    rejected_deterministic: int = 0
    rejected_llm: int = 0
    errors: int = 0

    @property
    def reject_rate(self) -> float:
        return (self.total - self.kept) / self.total if self.total else 0.0

    def summary(self) -> str:
        return (
            f"검증 {self.total}건 → 유지 {self.kept}건 "
            f"(기각률 {self.reject_rate:.1%}: "
            f"결정론적 {self.rejected_deterministic}, LLM {self.rejected_llm}, 오류 {self.errors})"
        )


class ReviewFilter:
    """생성된 지적을 재판정해 근거 없는 것을 걷어낸다."""

    def __init__(
        self,
        client: LLMClient,
        chunks: dict[str, ReviewChunk],
        *,
        require_changed_line: bool = True,
        max_workers: int = 4,
        exclude_rules: list[str] | set[str] | None = None,
    ) -> None:
        self.client = client
        self.chunks = chunks
        #: diff 리뷰에서는 변경되지 않은 라인에 대한 지적을 기각한다.
        #: 전체 파일 감사(`scan`) 모드에서는 False 로 둔다.
        self.require_changed_line = require_changed_line
        self.max_workers = max_workers
        self.exclude_rules = set(exclude_rules) if exclude_rules else set()
        self.stats = FilterStats()
        #: 검증 호출 자체가 실패한 건. 기각과는 구분해서 올린다 — 전자는 설비
        #: 고장이고 후자는 정상 동작이다.
        self.errors: list[str] = []
        self._errors_lock = threading.Lock()

    def filter(self, findings: list[Finding]) -> tuple[list[Finding], list[FilterVerdict]]:
        """유지된 지적과 기각 판정 목록을 돌려준다."""
        self.stats = FilterStats(total=len(findings))
        self.errors = []
        if not findings:
            return [], []

        # 1단계: 결정론적 검사. LLM 을 부르지 않고 끝나는 것들을 먼저 쳐낸다.
        candidates: list[tuple[Finding, ReviewChunk]] = []
        rejected: list[FilterVerdict] = []
        seen: set[tuple[str, int, str]] = set()

        for finding in findings:
            verdict = self._check_deterministic(finding, seen)
            if verdict is not None:
                rejected.append(verdict)
                self.stats.rejected_deterministic += 1
                continue
            chunk = self._resolve_chunk(finding)
            assert chunk is not None  # _check_deterministic 이 이미 확인했다
            seen.add((finding.path, finding.line, finding.rule_id))
            candidates.append((finding, chunk))

        if not candidates:
            return [], rejected

        # 2단계: LLM 재판정. 서로 독립적이므로 병렬로 던진다.
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            verdicts = list(pool.map(lambda pair: self._verify_llm(*pair), candidates))

        kept: list[Finding] = []
        for verdict in verdicts:
            if verdict.kept:
                kept.append(verdict.finding)
            else:
                rejected.append(verdict)
                if verdict.reject_reason is RejectReason.FILTER_ERROR:
                    self.stats.errors += 1
                else:
                    self.stats.rejected_llm += 1

        self.stats.kept = len(kept)
        log.info("%s", self.stats.summary())
        return kept, rejected

    # -- 1단계: 결정론적 -----------------------------------------------------

    def _check_deterministic(
        self, finding: Finding, seen: set[tuple[str, int, str]]
    ) -> FilterVerdict | None:
        """기각이면 판정을, 통과면 None 을 돌려준다."""
        key = (finding.path, finding.line, finding.rule_id)
        if key in seen:
            return FilterVerdict(
                finding, False, "동일 라인·동일 룰의 중복 지적",
                RejectReason.DUPLICATE, short_circuited=True,
            )

        if self.exclude_rules and is_rule_excluded(
            finding.rule_id, self.exclude_rules, path=finding.path
        ):
            return FilterVerdict(
                finding, False,
                f"제외 규칙에 지정된 룰입니다 ({finding.rule_id})",
                RejectReason.RULE_EXCLUDED, short_circuited=True,
            )

        chunk = self._resolve_chunk(finding)
        if chunk is None:
            return FilterVerdict(
                finding, False,
                f"{finding.path}:{finding.line} 를 포함하는 리뷰 청크가 없습니다 — 라인 번호 환각",
                RejectReason.LINE_OUT_OF_RANGE, short_circuited=True,
            )

        if not chunk.covers(finding.line):
            return FilterVerdict(
                finding, False,
                f"지적 라인 {finding.line} 이 청크 범위 "
                f"[{chunk.start_line}-{chunk.end_line}] 밖입니다",
                RejectReason.LINE_OUT_OF_RANGE, short_circuited=True,
            )

        if self.require_changed_line and finding.line not in chunk.changed_linenos:
            return FilterVerdict(
                finding, False,
                f"라인 {finding.line} 은 이번 변경에 포함되지 않았습니다 (diff 리뷰 범위 밖)",
                RejectReason.LINE_NOT_CHANGED, short_circuited=True,
            )

        return None

    def _resolve_chunk(self, finding: Finding) -> ReviewChunk | None:
        """지적이 속한 청크를 찾는다.

        생성 단계가 chunk_id 를 붙여주지만, `ocr` 위임 모드처럼 외부 도구가 만든
        지적에는 없을 수 있다. 그럴 땐 경로와 라인으로 역추적한다.
        """
        if finding.chunk_id:
            chunk = self.chunks.get(finding.chunk_id)
            if chunk is not None:
                return chunk

        for chunk in self.chunks.values():
            if chunk.path == finding.path and chunk.covers(finding.line):
                return chunk
        return None

    # -- 2단계: LLM 재판정 ---------------------------------------------------

    def _resolve_related_context(
        self, finding: Finding, chunk: ReviewChunk, window: int = 25
    ) -> str:
        """지적 라인 주변 창(±window) 밖에 정의된 연관 피호출 함수·클래스 정의를 발췌한다."""
        symbol_pool: dict[str, tuple[str, str]] = {}
        for other in self.chunks.values():
            for sym_name, snippet in other.symbol_definitions.items():
                symbol_pool.setdefault(sym_name, (other.path, snippet))
        for sym_name, snippet in chunk.symbol_definitions.items():
            symbol_pool[sym_name] = (chunk.path, snippet)

        if not symbol_pool and not chunk.flow_context:
            return ""

        min_ln = finding.line - window
        max_ln = finding.line + window

        # 지적 대상 라인(±2줄)의 원문 텍스트 확보
        nearby_code = " ".join(
            ln.text
            for ln in chunk.lines
            if finding.line - 2 <= ln.lineno <= finding.line + 2
        )
        claim_text = f"{finding.message} {finding.suggestion or ''}"
        call_bares = {c.split("(")[0].rsplit(".", 1)[-1].rsplit("::", 1)[-1].rsplit("->", 1)[-1] for c in chunk.calls}

        scored: list[tuple[int, str, str]] = []
        seen_snippets: set[str] = set()

        for sym_name, (sym_path, snippet) in symbol_pool.items():
            if not sym_name or not snippet or snippet in seen_snippets:
                continue

            # 동일 파일이고 정의 라인들이 모두 현재 ±window 안에 이미 포함되어 있으면 중복 제외
            lineno_matches = [int(m) for m in re.findall(r"(?:^|\n)L(\d+):", snippet)]
            has_foreign_header = snippet.startswith("// ") and not snippet.startswith(f"// {chunk.path}:")
            if (
                sym_path == chunk.path
                and not has_foreign_header
                and lineno_matches
                and min(lineno_matches) >= min_ln
                and max(lineno_matches) <= max_ln
            ):
                continue

            bare = sym_name.split("(")[0].rsplit("::", 1)[-1].rsplit(".", 1)[-1].rsplit("->", 1)[-1]
            if not bare:
                continue

            score = 0
            pat = re.compile(rf"\b{re.escape(bare)}\b")
            if pat.search(nearby_code):
                score += 4
            if pat.search(claim_text):
                score += 3
            if bare in call_bares or sym_name in chunk.calls:
                score += 2
            if chunk.enclosing_symbol and bare in chunk.enclosing_symbol:
                score += 1

            if score > 0:
                seen_snippets.add(snippet)
                scored.append((score, sym_name, snippet))

        scored.sort(key=lambda item: (-item[0], item[1]))
        parts: list[str] = []
        for _, sym_name, snippet in scored[:3]:
            parts.append(f"### `{sym_name}`\n```{chunk.language.value}\n{snippet}\n```")

        if chunk.flow_context:
            parts.append(chunk.flow_context)

        if not parts:
            return ""

        body = "\n\n".join(parts)
        return f"\n## 연관 심볼 및 피호출 함수 정의 (창 외부 문맥)\n{body}\n"

    def _verify_llm(self, finding: Finding, chunk: ReviewChunk) -> FilterVerdict:
        suggestion_block = (
            f"- 제안된 수정: {finding.suggestion}\n" if finding.suggestion else ""
        )
        symbol_line = (
            f"둘러싼 심볼: {chunk.enclosing_symbol}" if chunk.enclosing_symbol else ""
        )
        code_window = chunk.render_window(finding.line, window=25)
        related_block = self._resolve_related_context(finding, chunk, window=25)

        user = VERIFIER_USER.format(
            path=finding.path,
            symbol_line=symbol_line,
            lang=chunk.language.value,
            code=code_window,
            related_block=related_block,
            line=finding.line,
            dimension=finding.dimension.value,
            severity=finding.severity.value,
            rule_id=finding.rule_id,
            message=finding.message,
            suggestion_block=suggestion_block,
        )

        # 검증기 입력 토큰 예산 초과 방어: 예산 초과 시 창 외부 보조 문맥을 생략하여 핵심 코드 창을 보호
        max_in = getattr(self.client.config, "max_input_tokens", 4096) if hasattr(self.client, "config") else 4096
        verifier_budget = max(max_in - estimate_tokens(VERIFIER_SYSTEM) - 250, 512)
        if related_block and estimate_tokens(user) > verifier_budget:
            user = VERIFIER_USER.format(
                path=finding.path,
                symbol_line=symbol_line,
                lang=chunk.language.value,
                code=code_window,
                related_block="",
                line=finding.line,
                dimension=finding.dimension.value,
                severity=finding.severity.value,
                rule_id=finding.rule_id,
                message=finding.message,
                suggestion_block=suggestion_block,
            )

        try:
            # 예산은 설정(llm.verifier.max_output_tokens)이 정한다 — 검증용으로
            # 상한이 이미 조여져 있다. 여기에 숫자를 박으면 그 설정이 죽는다.
            response = self.client.complete_json(
                VERIFIER_SYSTEM, user, VERDICT_SCHEMA, schema_name="verdict",
            )
        except Exception as exc:  # noqa: BLE001 - 검증 실패는 보수적으로 기각한다
            log.warning("검증 호출 실패 (%s:%d): %s", finding.path, finding.line, exc)
            with self._errors_lock:
                self.errors.append(f"{finding.path}:{finding.line} 검증 실패: {exc}")
            return FilterVerdict(
                finding, False, f"검증 호출 실패로 보수적 기각: {exc}",
                RejectReason.FILTER_ERROR,
            )

        reason = str(response.get("reason", "")).strip()

        if not response.get("code_present", True):
            return FilterVerdict(
                finding, False, reason or "지적이 묘사하는 코드가 스니펫에 없습니다",
                RejectReason.CODE_NOT_FOUND,
            )

        if response.get("verdict") != "yes":
            return FilterVerdict(
                finding, False, reason or "검증자가 근거 부족으로 기각",
                RejectReason.VERDICT_NO,
            )

        finding.verifier_comment = reason or "검증 통과"
        return FilterVerdict(finding, True, reason or "검증 통과")
