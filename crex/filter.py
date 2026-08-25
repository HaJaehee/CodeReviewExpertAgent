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
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from .llm import LLMClient
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
            "maxLength": 300,
            "description": "판정 근거 1~2문장",
        },
    },
    "required": ["verdict", "code_present", "reason"],
    "additionalProperties": False,
}


VERIFIER_SYSTEM = """\
당신은 코드리뷰 지적을 검증하는 심사관이다. 리뷰어가 아니다.

주어진 코드 스니펫만을 근거로, 제시된 지적이 타당한지 판정하라.
스니펫의 각 줄에는 `[added @142]` 형태로 변경 상태와 실제 라인 번호가 붙어 있다.

다음 경우 반드시 "no" 로 판정하라.
- 지적이 묘사하는 코드가 스니펫에 존재하지 않는다 (환각)
- 지적이 참조하는 함수/변수/API 가 스니펫에 없다
- 지적의 전제가 코드와 다르다 (예: "널 체크가 없다"고 했으나 실제로는 있다)
- 스니펫만으로는 참인지 알 수 없다 (외부 파일을 봐야 판단 가능)
- 실제 동작에 영향이 없는 취향 문제다

다음 경우에만 "yes" 로 판정하라.
- 지적된 결함이 스니펫 안에서 명확히 확인된다
- 지적된 라인의 코드가 실제로 그 문제를 갖고 있다

확신이 서지 않으면 "no" 다. 놓치는 것보다 잘못 지적하는 쪽이 훨씬 해롭다.
근거(reason)는 한국어 1~2문장으로 쓴다.
"""


VERIFIER_USER = """\
## 코드 스니펫
파일: {path}
{symbol_line}
```{lang}
{code}
```

## 검증할 지적
- 위치: {path}:{line}
- 분류: {dimension} / {severity}
- 룰: {rule_id}
- 내용: {message}
{suggestion_block}
이 지적이 타당한가?
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
    ) -> None:
        self.client = client
        self.chunks = chunks
        #: diff 리뷰에서는 변경되지 않은 라인에 대한 지적을 기각한다.
        #: 전체 파일 감사(`scan`) 모드에서는 False 로 둔다.
        self.require_changed_line = require_changed_line
        self.max_workers = max_workers
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

        chunk = self._resolve_chunk(finding)
        if chunk is None:
            return FilterVerdict(
                finding, False,
                f"{finding.path}:{finding.line} 를 포함하는 리뷰 청크가 없다 — 라인 번호 환각",
                RejectReason.LINE_OUT_OF_RANGE, short_circuited=True,
            )

        if not chunk.covers(finding.line):
            return FilterVerdict(
                finding, False,
                f"지적 라인 {finding.line} 이 청크 범위 "
                f"[{chunk.start_line}-{chunk.end_line}] 밖이다",
                RejectReason.LINE_OUT_OF_RANGE, short_circuited=True,
            )

        if self.require_changed_line and finding.line not in chunk.changed_linenos:
            return FilterVerdict(
                finding, False,
                f"라인 {finding.line} 은 이번 변경에 포함되지 않았다 (diff 리뷰 범위 밖)",
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

    def _verify_llm(self, finding: Finding, chunk: ReviewChunk) -> FilterVerdict:
        suggestion_block = (
            f"- 제안된 수정: {finding.suggestion}\n" if finding.suggestion else ""
        )
        symbol_line = (
            f"둘러싼 심볼: {chunk.enclosing_symbol}" if chunk.enclosing_symbol else ""
        )
        user = VERIFIER_USER.format(
            path=finding.path,
            symbol_line=symbol_line,
            lang=chunk.language.value,
            code=chunk.render_code(),
            line=finding.line,
            dimension=finding.dimension.value,
            severity=finding.severity.value,
            rule_id=finding.rule_id,
            message=finding.message,
            suggestion_block=suggestion_block,
        )

        try:
            response = self.client.complete_json(
                VERIFIER_SYSTEM, user, VERDICT_SCHEMA,
                schema_name="verdict", max_output_tokens=200,
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
                finding, False, reason or "지적이 묘사하는 코드가 스니펫에 없다",
                RejectReason.CODE_NOT_FOUND,
            )

        if response.get("verdict") != "yes":
            return FilterVerdict(
                finding, False, reason or "검증자가 근거 부족으로 기각",
                RejectReason.VERDICT_NO,
            )

        return FilterVerdict(finding, True, reason or "검증 통과")
