"""핵심 데이터 모델.

폐쇄망 반입을 고려해 stdlib dataclass만 사용한다 (pydantic 등 외부 의존 없음).
모든 모델은 `to_dict()` / `from_dict()` 로 JSONL 왕복이 가능해야 한다.
평가 하네스(eval/run_eval.py)가 이 형식을 그대로 읽는다.
"""

from __future__ import annotations

import dataclasses
import enum
from dataclasses import dataclass, field
from typing import Any


class Language(str, enum.Enum):
    CPP = "cpp"
    CSHARP = "csharp"
    PYTHON = "python"
    OTHER = "other"

    @classmethod
    def from_path(cls, path: str) -> "Language":
        lower = path.lower()
        for suffix in (".cpp", ".cc", ".cxx", ".c", ".h", ".hpp", ".hh", ".hxx", ".inl"):
            if lower.endswith(suffix):
                return cls.CPP
        if lower.endswith(".cs"):
            return cls.CSHARP
        if lower.endswith((".py", ".pyi")):
            return cls.PYTHON
        return cls.OTHER


class Dimension(str, enum.Enum):
    """BitsAI-CR 의 4개 최상위 리뷰 차원."""

    DEFECT = "code_defect"
    SECURITY = "security_vulnerability"
    MAINTAINABILITY = "maintainability"
    PERFORMANCE = "performance"


class Severity(str, enum.Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class LineStatus(str, enum.Enum):
    """청크 내 각 라인의 변경 상태.

    프롬프트에 `[added @142]` 형태로 직접 주석되어, 모델이 라인 번호를
    '기억'할 필요를 없앤다. 라인 번호 환각을 구조적으로 차단하는 장치다.
    """

    ADDED = "added"
    DELETED = "deleted"
    UNCHANGED = "unchanged"


# --------------------------------------------------------------------------
# diff 원본
# --------------------------------------------------------------------------


@dataclass
class DiffLine:
    status: LineStatus
    #: 삭제 라인은 구(舊) 파일 기준, 그 외는 신(新) 파일 기준 1-indexed 라인 번호.
    lineno: int
    text: str

    def annotate(self) -> str:
        """프롬프트에 주입할 주석 형태로 렌더한다."""
        return f"[{self.status.value} @{self.lineno}] {self.text}"


@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[DiffLine] = field(default_factory=list)

    @property
    def new_end(self) -> int:
        return self.new_start + max(self.new_count, 1) - 1

    def added_linenos(self) -> set[int]:
        return {ln.lineno for ln in self.lines if ln.status is LineStatus.ADDED}


@dataclass
class FileDiff:
    path: str
    old_path: str | None = None
    is_new: bool = False
    is_deleted: bool = False
    is_binary: bool = False
    hunks: list[Hunk] = field(default_factory=list)

    @property
    def language(self) -> Language:
        return Language.from_path(self.path)


# --------------------------------------------------------------------------
# 정적분석 그라운딩
# --------------------------------------------------------------------------

#: 프롬프트에 그대로 들어가는 문장이다. 사람이 읽는 화면 글이 아니라 **모델에게
#: 주는 지시**라, 사용자 대상 글의 합쇼체 규칙을 따르지 않는다. 이름 끝의
#: `_PROMPT` 가 그 표시이고, 말투 검사(`tests/test_cli.py`)가 그것으로 걸러 낸다.
STATIC_FINDING_HINT_PROMPT = (
    "→ 검토 지침: 이 정적분석 지적이 타당하다면 해당 라인의 결함으로 적극 채택하고 "
    "적합한 룰 ID를 부여하라."
)

#: 같은 이유로 모델에게 주는 문장이다. "아무것도 못 찾았다"를 모델이 오해하지
#: 않도록 범위를 밝혀 준다.
NO_STATIC_FINDINGS_PROMPT = "(없음 — 정적분석 도구가 이 범위에서 아무것도 보고하지 않았다)"


@dataclass
class StaticFinding:
    """결정론적 도구가 확인한 사실. LLM 은 이것을 *검증*할 뿐 지어내지 않는다."""

    tool: str
    path: str
    line: int
    rule_id: str
    message: str
    severity: Severity = Severity.MEDIUM
    column: int | None = None

    def render(self) -> str:
        loc = f"{self.path}:{self.line}"
        if self.column:
            loc += f":{self.column}"
        return (
            f"- [{self.tool}:{self.rule_id}] {loc} (심각도: {self.severity.value})\n"
            f"  내용: {self.message}\n"
            f"  {STATIC_FINDING_HINT_PROMPT}"
        )

    def to_dict(self) -> dict[str, Any]:
        return _asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StaticFinding":
        return cls(
            tool=data["tool"],
            path=data["path"],
            line=int(data["line"]),
            rule_id=data.get("rule_id", ""),
            message=data.get("message", ""),
            severity=Severity(data.get("severity", "medium")),
            column=data.get("column"),
        )


# --------------------------------------------------------------------------
# 리뷰 단위
# --------------------------------------------------------------------------


@dataclass
class ReviewChunk:
    """LLM 한 번의 호출에 들어가는 리뷰 단위.

    hunk 를 tree-sitter 함수 경계까지 확장한 뒤, 원본 diff 대비 확장 배수에
    상한(기본 4배, 초과 시 3배 절단)을 걸어 컨텍스트 폭주를 막는다.
    """

    chunk_id: str
    path: str
    language: Language
    #: 신 파일 기준 청크가 커버하는 라인 범위 (1-indexed, 양끝 포함).
    start_line: int
    end_line: int
    lines: list[DiffLine] = field(default_factory=list)
    #: 이 청크에 실제로 변경이 일어난 라인 (리뷰 지적이 허용되는 유일한 범위).
    changed_linenos: set[int] = field(default_factory=set)
    #: 확장 근거가 된 심볼 이름 (예: "MyClass::Update"). 프롬프트 헤더에 쓴다.
    enclosing_symbol: str | None = None
    static_findings: list[StaticFinding] = field(default_factory=list)
    ast_context: str | None = None

    def render_code(self) -> str:
        return "\n".join(line.annotate() for line in self.lines)

    def render_window(self, target_line: int, window: int = 25) -> str:
        """target_line 주변 ±window 범위의 라인만 발췌하여 렌더한다 (검증기 컨텍스트 최적화)."""
        min_ln = target_line - window
        max_ln = target_line + window
        selected = [ln for ln in self.lines if min_ln <= ln.lineno <= max_ln]
        if not selected:
            return self.render_code()
        return "\n".join(ln.annotate() for ln in selected)

    def render_static_findings(self, max_items: int = 6) -> str:
        if not self.static_findings:
            return NO_STATIC_FINDINGS_PROMPT

        # 변경 라인과 직접 연관된 지적 우선 정렬
        relevant = [f for f in self.static_findings if f.line in self.changed_linenos]
        others = [f for f in self.static_findings if f.line not in self.changed_linenos]
        findings = (relevant + others)[:max_items]

        rendered = [f.render() for f in findings]
        if len(self.static_findings) > max_items:
            rendered.append(f"... (외 {len(self.static_findings) - max_items}건 정적분석 지적 생략)")
        return "\n".join(rendered)

    def render_ast_context(self) -> str:
        if self.ast_context:
            return self.ast_context
        if self.enclosing_symbol:
            return f"- 둘러싼 심볼: `{self.enclosing_symbol}`"
        return "(Tree-sitter 구문 분석 정보 없음)"

    def covers(self, lineno: int) -> bool:
        return self.start_line <= lineno <= self.end_line


# --------------------------------------------------------------------------
# 리뷰 결과
# --------------------------------------------------------------------------


@dataclass
class Finding:
    """RuleChecker 가 생성한 지적 1건."""

    path: str
    line: int
    dimension: Dimension
    severity: Severity
    rule_id: str
    message: str
    #: 모델이 제시한 수정안 (선택).
    suggestion: str | None = None
    #: 지적이 나온 청크 id. 필터링 단계에서 원본 코드를 되찾는 데 쓴다.
    chunk_id: str | None = None
    #: 다중 라인 지적일 때의 끝 라인. 단일 라인이면 line 과 같다.
    end_line: int | None = None
    #: 검증기가 남긴 적극적인 평가/코멘트 (선택).
    verifier_comment: str | None = None

    def __post_init__(self) -> None:
        if self.end_line is None:
            self.end_line = self.line

    def to_dict(self) -> dict[str, Any]:
        return _asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Finding":
        return cls(
            path=data["path"],
            line=int(data["line"]),
            dimension=Dimension(data.get("dimension", "code_defect")),
            severity=Severity(data.get("severity", "medium")),
            rule_id=data.get("rule_id", ""),
            message=data.get("message", ""),
            suggestion=data.get("suggestion"),
            chunk_id=data.get("chunk_id"),
            end_line=data.get("end_line"),
            verifier_comment=data.get("verifier_comment"),
        )


class RejectReason(str, enum.Enum):
    """지적이 기각된 사유. 필터 튜닝의 근거 데이터가 되므로 반드시 기록한다."""

    #: 지적한 라인이 청크 범위 밖 — 명백한 라인 번호 환각.
    LINE_OUT_OF_RANGE = "line_out_of_range"
    #: 지적한 라인이 변경되지 않은 라인 — diff 리뷰 범위 위반.
    LINE_NOT_CHANGED = "line_not_changed"
    #: LLM 검증자가 근거 부족으로 기각.
    VERDICT_NO = "verdict_no"
    #: LLM 검증자가 해당 코드의 존재 자체를 부정.
    CODE_NOT_FOUND = "code_not_found"
    #: 동일 라인·동일 룰의 중복 지적.
    DUPLICATE = "duplicate"
    #: 검증 호출이 실패해 보수적으로 기각.
    FILTER_ERROR = "filter_error"


@dataclass
class FilterVerdict:
    """ReviewFilter 의 판정 1건."""

    finding: Finding
    kept: bool
    reason: str
    reject_reason: RejectReason | None = None
    #: 결정론적 검사만으로 판정이 끝나 LLM 호출을 생략했는지 여부.
    short_circuited: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding": self.finding.to_dict(),
            "kept": self.kept,
            "reason": self.reason,
            "reject_reason": self.reject_reason.value if self.reject_reason else None,
            "short_circuited": self.short_circuited,
        }


@dataclass
class ReviewResult:
    """파이프라인 1회 실행의 전체 결과."""

    kept: list[Finding] = field(default_factory=list)
    rejected: list[FilterVerdict] = field(default_factory=list)
    chunks_reviewed: int = 0
    static_findings_count: int = 0
    #: 단계별 소요 시간(초).
    timings: dict[str, float] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    #: LLM 호출이 실패한 건수. 파일 읽기 실패 등과 섞이지 않게 따로 센다.
    #: `healthy` 가 이것으로 "0건인데 정상인가"를 판정한다.
    generation_errors: int = 0
    verification_errors: int = 0

    @property
    def total_raw(self) -> int:
        return len(self.kept) + len(self.rejected)

    @property
    def healthy(self) -> bool:
        """이 실행을 믿어도 되는가.

        "지적 0건"은 두 가지를 뜻할 수 있다 — 코드가 깨끗하거나, 파이프라인이
        고장났거나. 리포트와 CLI 종료 코드가 그 둘을 다르게 다루도록 여기서
        한 번에 판정한다.
        """
        return not self.errors

    @property
    def reject_rate(self) -> float:
        """기각률. BitsAI-CR 실측 55.25% 를 기준선으로 삼는다."""
        return len(self.rejected) / self.total_raw if self.total_raw else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "kept": [f.to_dict() for f in self.kept],
            "rejected": [v.to_dict() for v in self.rejected],
            "chunks_reviewed": self.chunks_reviewed,
            "static_findings_count": self.static_findings_count,
            "total_raw": self.total_raw,
            "reject_rate": round(self.reject_rate, 4),
            "timings": {k: round(v, 3) for k, v in self.timings.items()},
            "errors": self.errors,
            "generation_errors": self.generation_errors,
            "verification_errors": self.verification_errors,
            "healthy": self.healthy,
        }


# --------------------------------------------------------------------------


def _asdict(obj: Any) -> dict[str, Any]:
    """enum 을 값으로 풀고 set 을 정렬된 list 로 바꾸는 dataclasses.asdict 래퍼."""

    def convert(value: Any) -> Any:
        if isinstance(value, enum.Enum):
            return value.value
        if isinstance(value, set):
            return sorted(value)
        if isinstance(value, dict):
            return {k: convert(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [convert(v) for v in value]
        return value

    return {k: convert(v) for k, v in dataclasses.asdict(obj).items()}
