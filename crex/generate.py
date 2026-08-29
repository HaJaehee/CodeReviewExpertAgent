"""RuleChecker — 청크 단위 지적 생성.

에이전트 루프를 쓰지 않는다. 25~40B 급 모델에게 툴을 쥐어주고 자율 탐색을
시키면 툴콜 실패와 컨텍스트 폭주가 함께 온다. 대신 고정 단계로 간다.
청크 하나 → 프롬프트 하나 → 지적 목록 하나.

## guided decoding 을 환각 방지에 쓴다

vLLM 의 스키마 강제는 보통 "JSON 파싱 실패를 없애는" 용도로 쓰이지만, 여기서는
그보다 강하게 쓴다. 스키마의 `enum` 에 **허용된 값만** 넣어두면 모델은 그 밖의
토큰을 아예 생성할 수 없다.

- `rule_id` → 택소노미에 존재하는 룰 ID 만 허용 → 룰 날조가 불가능해진다
- `line` → 이 청크에서 실제로 변경된 라인 번호만 허용 → **라인 번호 환각이
  사후 필터링 대상이 아니라 구조적으로 불가능해진다**

필터의 결정론적 검사는 이 방어를 뚫는 경우(구버전 vLLM, guided decoding 미지원
백엔드)를 위해 그대로 남겨둔다. 두 겹으로 막는다.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from .llm import LLMClient
from .rules import Rule, Taxonomy, render_rules_for_prompt
from .schema import Finding, ReviewChunk, Severity

log = logging.getLogger(__name__)

#: 청크 하나에서 받을 지적 개수 상한. 모델이 지적을 흩뿌리는 것을 막는다.
#: 정말 문제가 많은 코드라면 상위 몇 건만 봐도 리뷰어가 알아차린다.
MAX_FINDINGS_PER_CHUNK = 5


RULECHECKER_SYSTEM = """\
당신은 엄격하고 능동적인 시니어 코드리뷰어다. 제시된 코드 조각에서 실제 결함과 잠재적 버그, 취약점을 적극적으로 찾아내고 지적한다.

## 입력 형식
코드의 각 줄에는 `[added @142]` 형태로 변경 상태와 실제 라인 번호가 붙어 있다.
- added: 이번 변경으로 추가된 줄 (주요 리뷰 및 지적 대상)
- deleted: 이번 변경으로 삭제된 줄 (참고용 — 지적 대상이 아니다)
- unchanged: 문맥으로 제공된 줄 (지적 대상이 아니다)

## Tree-sitter AST 구문 분석 활용
함께 제공되는 Tree-sitter 구문 분석 결과를 적극적으로 활용하라.
- 변경된 라인에서 호출된 함수/메서드, 변수 및 포인터의 선언과 참조 순서, 리소스 할당 및 해제 패턴, 제어문 분기 조건을 AST 수준에서 명확히 파악하라.
- 예: 포인터 취득 후 크기 재할당(resize/realloc) 호출이 이어지는지, 동적 할당 후 해제 누락이 있는지, 널 포인터 체크 없는 참조가 있는지 등을 AST 노드 흐름을 통해 면밀히 검토하라.

## 정적분석 결과의 적극적 반영
이미 실행된 정적분석 도구 결과가 함께 제공된다.
1. 도구가 보고한 항목이 실제 코드에서 문제인지 면밀히 확인한다.
2. 실제 결함(버그, 널 역참조, 누수, 자원 무효화, 보안 결함 등)인 경우, 적용할 룰 목록에서 가장 적합한 룰 ID를 부여하여 **반드시 findings 지적에 포함하라**. 정당한 사유(명백한 오탐 등) 없이 정적분석 결과가 있는데도 이를 누락하거나 '문제 없음'으로 넘겨서는 안 된다.
3. 정적분석 도구가 구조적으로 잡을 수 없는 것 — 로직 오류, 설계 결함, 엣지 케이스 처리 누락, 의도와 구현의 불일치, 동시성/자원 생명주기 문제 — 도 AST 구조를 바탕으로 적극적으로 찾아내어 지적하라.

## 절대 규칙
- **added 로 표시된 줄만 지적한다.** 다른 줄은 문맥일 뿐이다.
- 제시된 코드와 문맥에서 확인되는 결함을 적극적으로 지적한다.
- 주어진 룰 목록에서 가장 부합하는 룰 ID를 선택한다.
- 발견된 결함이 있다면 주저하지 말고 findings 목록에 추가하라. 결함이 전혀 없을 때만 빈 목록을 반환하라.
- 코드가 무엇을 하는지 단순 설명하지 않고, 무엇이 왜 위험하고 결함인지 명확히 짚는다.

## 출력
message 는 한국어로, 무엇이 왜 문제이고 어떤 위험이 있는지 두 문장 이내로 명확히 쓴다.
suggestion 에는 고칠 코드를 쓴다 (없으면 빈 문자열).
"""


RULECHECKER_USER = """\
## 검토할 코드
파일: {path} ({lang})
{symbol_line}
```
{code}
```

## Tree-sitter 구문 분석 (AST)
{ast_context}

## 정적분석 도구 결과
{static_findings}

## 적용할 룰
{rules}

## 지시
1. **정적분석 결과 반영**: 정적분석 도구가 결함을 보고했고 이것이 실제 문제라면, 적용할 룰 목록에서 가장 적합한 `rule_id`를 선택하여 반드시 지적에 포함하라.
2. **Tree-sitter AST 기반 결함 분석**: 구문 분석 결과(호출 함수, 변수 선언/사용, 제어 흐름)와 added 줄을 면밀히 분석하여, 정적분석기가 놓친 잠재적 결함(포인터/자원 수명주기, 로직 오류, 널/예외 미처리 등)을 적극적으로 지적하라.
3. 발견된 결함이 있다면 findings 에 추가하라. 결함이 전혀 없을 때만 빈 목록을 반환하라.
"""


class RuleChecker:
    """청크마다 LLM 을 한 번 호출해 지적을 생성한다."""

    def __init__(
        self,
        client: LLMClient,
        taxonomy: Taxonomy,
        *,
        max_findings: int = MAX_FINDINGS_PER_CHUNK,
        max_workers: int = 4,
        restrict_lines: bool = True,
    ) -> None:
        self.client = client
        self.taxonomy = taxonomy
        self.max_findings = max_findings
        self.max_workers = max_workers
        #: line 을 변경된 라인 집합으로 제한할지. 전체 파일 감사 모드에서는 끈다.
        self.restrict_lines = restrict_lines
        #: 청크별 호출 실패. 파이프라인이 결과에 실어 사용자에게 보여준다.
        #: 이걸 로그에만 남기면 "지적 0건"과 "전부 실패"가 구분되지 않는다.
        self.errors: list[str] = []
        self._errors_lock = threading.Lock()

    def review(self, chunks: list[ReviewChunk]) -> list[Finding]:
        self.errors = []
        if not chunks:
            return []
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            batches = pool.map(self.review_chunk, chunks)
        return [finding for batch in batches for finding in batch]

    def review_chunk(self, chunk: ReviewChunk) -> list[Finding]:
        rules = self.taxonomy.for_language(chunk.language)
        if not rules:
            log.debug("%s: 적용할 룰이 없습니다 (%s)", chunk.chunk_id, chunk.language.value)
            return []

        allowed_lines = self._allowed_lines(chunk)
        if not allowed_lines:
            log.debug("%s: 지적 가능한 라인이 없습니다", chunk.chunk_id)
            return []

        schema = build_findings_schema(
            rule_ids=[r.id for r in rules],
            allowed_lines=allowed_lines if self.restrict_lines else None,
            max_findings=self.max_findings,
        )
        user = RULECHECKER_USER.format(
            path=chunk.path,
            lang=chunk.language.value,
            symbol_line=f"둘러싼 심볼: {chunk.enclosing_symbol}" if chunk.enclosing_symbol else "",
            code=chunk.render_code(),
            ast_context=chunk.render_ast_context(),
            static_findings=chunk.render_static_findings(),
            rules=render_rules_for_prompt(rules),
        )

        try:
            response = self.client.complete_json(
                RULECHECKER_SYSTEM, user, schema,
                schema_name="findings", max_output_tokens=900,
            )
        except Exception as exc:  # noqa: BLE001 - 청크 하나가 실패해도 나머지는 진행한다
            log.warning("%s 리뷰 실패: %s", chunk.chunk_id, exc)
            self._record_error(f"{chunk.chunk_id} 생성 실패: {exc}")
            return []

        return self._parse(response, chunk, rules)

    def _record_error(self, message: str) -> None:
        with self._errors_lock:
            self.errors.append(message)

    def _allowed_lines(self, chunk: ReviewChunk) -> list[int]:
        if self.restrict_lines:
            return sorted(chunk.changed_linenos)
        return list(range(chunk.start_line, chunk.end_line + 1))

    def _parse(self, response: dict, chunk: ReviewChunk, rules: list[Rule]) -> list[Finding]:
        by_id = {rule.id: rule for rule in rules}
        findings: list[Finding] = []

        for item in response.get("findings", [])[: self.max_findings]:
            rule = by_id.get(str(item.get("rule_id", "")))
            if rule is None:
                # guided decoding 이 켜져 있으면 도달하지 않는다. 꺼진 환경 대비.
                log.debug("%s: 알 수 없는 룰 ID %r 무시", chunk.chunk_id, item.get("rule_id"))
                continue

            message = str(item.get("message", "")).strip()
            if not message:
                continue

            try:
                line = int(item["line"])
            except (KeyError, TypeError, ValueError):
                continue

            suggestion = str(item.get("suggestion", "")).strip()
            findings.append(
                Finding(
                    path=chunk.path,
                    line=line,
                    dimension=rule.dimension,
                    # 심각도는 택소노미가 정한다. 모델이 정하게 두면 전부 high 가 된다.
                    severity=_severity_of(item.get("severity"), rule.severity),
                    rule_id=rule.id,
                    message=message,
                    suggestion=suggestion or None,
                    chunk_id=chunk.chunk_id,
                )
            )

        return findings


def build_findings_schema(
    *,
    rule_ids: list[str],
    allowed_lines: list[int] | None,
    max_findings: int = MAX_FINDINGS_PER_CHUNK,
) -> dict:
    """지적 목록 스키마를 만든다.

    `rule_ids` 와 `allowed_lines` 를 enum 으로 박아 넣는 것이 핵심이다.
    guided decoding 이 이 범위 밖의 토큰을 생성 자체에서 막는다.
    """
    line_schema: dict = {"type": "integer", "description": "지적 대상 라인 번호"}
    if allowed_lines:
        # 변경된 라인만 허용 → 라인 번호 환각이 생성 단계에서 불가능해진다.
        line_schema = {"type": "integer", "enum": allowed_lines}

    return {
        "type": "object",
        "properties": {
            "findings": {
                "type": "array",
                "maxItems": max_findings,
                "items": {
                    "type": "object",
                    "properties": {
                        "line": line_schema,
                        "rule_id": {"type": "string", "enum": rule_ids},
                        "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                        "message": {"type": "string", "maxLength": 400},
                        "suggestion": {"type": "string", "maxLength": 400},
                    },
                    "required": ["line", "rule_id", "severity", "message"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["findings"],
        "additionalProperties": False,
    }


def _severity_of(raw: object, default: Severity) -> Severity:
    """모델이 올린 심각도는 택소노미 값을 넘지 못하게 한다.

    소형 모델은 자기가 찾은 것을 전부 high 로 매기는 경향이 있다. 택소노미가
    정한 값을 상한으로 두어 우선순위가 무의미해지는 것을 막는다.
    """
    order = {Severity.LOW: 0, Severity.MEDIUM: 1, Severity.HIGH: 2}
    try:
        reported = Severity(str(raw))
    except ValueError:
        return default
    return reported if order[reported] < order[default] else default
