"""이벤트 모델과 추적기 — Engine 계층.

파이프라인 한 번의 실행을 시간순 이벤트 목록으로 바꾼다. 화면은 이 목록만
읽으므로, 여기 없는 것은 화면에도 없다.

## 상관관계를 프롬프트 본문으로 맞춘다

생성·검증 호출은 `LLMClient.complete_json(system, user, schema)` 로 들어온다.
클라이언트 입장에서 보이는 건 문자열 두 개뿐이라, 이 호출이 어느 청크의
것인지가 그대로는 드러나지 않는다. `RuleChecker` 는 청크를 스레드풀에 흩뿌리므로
스레드로 짚을 수도 없다.

프롬프트 템플릿을 정규식으로 되파싱하는 방법은 쓰지 않았다. 템플릿이 바뀌는
순간 조용히 어긋나고, 그 사실을 아무도 모른다. 대신 **청크가 렌더한 코드
문자열이 프롬프트 안에 그대로 들어있다**는 사실을 쓴다.

    chunk.render_code() in user_prompt        # 생성·검증 양쪽 모두 참
    finding.message     in user_prompt        # 검증 프롬프트에 원문 그대로

템플릿을 어떻게 고치든 코드와 지적 원문이 프롬프트에 들어가는 한 성립한다.
못 맞추면 `chunk_id=None` 인 이벤트가 나갈 뿐, 리뷰 자체는 영향받지 않는다.
계측이 리뷰를 망가뜨리지 않는 것이 이 모듈의 유일한 불변식이다.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..schema import ReviewChunk, ReviewResult

#: 실행 하나가 남길 수 있는 이벤트 상한. 폴더 감사처럼 청크가 수백 개인 실행에서
#: 브라우저와 서버 메모리를 동시에 지키는 선. 넘으면 뒤를 버리고 표시만 남긴다.
MAX_EVENTS = 4000

#: 이벤트에 실을 프롬프트·응답 본문 길이 상한. 입력 예산이 8192 토큰이라
#: 프롬프트 하나가 24KB 를 넘을 수 있고, 청크마다 두 번씩 쌓인다.
MAX_TEXT = 24000

#: 상한에 걸려도 반드시 통과시키는 이벤트. 실행의 결말을 버리면 상한의 의미가
#: 뒤집힌다 — 호출 카드 몇 개를 잃는 것과 판정 전체를 잃는 것은 다른 문제다.
TERMINAL_EVENTS = frozenset({"run.finished", "run.failed", "run.cancelling"})


@dataclass(frozen=True)
class Event:
    """실행 타임라인의 한 점."""

    seq: int
    #: 실행 시작 기준 경과 초. 벽시계 시각이 아니라 경과를 쓰는 이유는
    #: 브라우저와 서버의 시계가 다를 수 있어서다.
    at: float
    type: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "at": round(self.at, 3), "type": self.type, "data": self.data}


def clip(text: str, limit: int = MAX_TEXT) -> str:
    """이벤트에 실을 만큼만 남긴다. 잘렸다는 사실을 본문에 남긴다."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n... ({len(text) - limit}자 생략) ..."


class Tracer:
    """이벤트를 모으고, LLM 호출을 청크·지적에 이어 붙인다.

    스레드 안전해야 한다. 생성과 검증 모두 스레드풀에서 병렬로 들어온다.
    """

    def __init__(self, sink: Callable[[Event], None] | None = None) -> None:
        self._lock = threading.Lock()
        self._started = time.perf_counter()
        self._seq = 0
        self._calls = 0
        self._dropped = 0
        self.events: list[Event] = []
        self.sink = sink
        #: 상관관계용. 렌더한 코드는 청크마다 한 번만 만든다 (호출마다 만들면
        #: 청크 400줄 × 호출 수만큼 문자열이 다시 생긴다).
        self._chunks: list[tuple[str, ReviewChunk]] = []
        self._findings: list[tuple[str, dict[str, Any]]] = []
        self._chunks_emitted = False
        self.result: ReviewResult | None = None

    # -- 이벤트 -------------------------------------------------------------

    def emit(self, type_: str, **data: Any) -> Event | None:
        with self._lock:
            if len(self.events) >= MAX_EVENTS and type_ not in TERMINAL_EVENTS:
                self._dropped += 1
                return None
            self._seq += 1
            event = Event(self._seq, time.perf_counter() - self._started, type_, data)
            self.events.append(event)
        if self.sink is not None:
            self.sink(event)
        return event

    def since(self, seq: int) -> list[Event]:
        with self._lock:
            return [e for e in self.events if e.seq > seq]

    @property
    def dropped(self) -> int:
        with self._lock:
            return self._dropped

    def next_call_id(self) -> int:
        """LLM 호출 식별자. 요청·응답 이벤트를 짝짓는 데만 쓴다."""
        with self._lock:
            self._calls += 1
            return self._calls

    # -- 단계 ---------------------------------------------------------------

    def stage_started(self, stage: str) -> None:
        self.emit("stage.started", stage=stage)

    def stage_finished(self, stage: str, seconds: float | None) -> None:
        self.emit("stage.finished", stage=stage, seconds=round(seconds or 0.0, 3))

    # -- 청크 ---------------------------------------------------------------

    def plan(self, chunks: list[ReviewChunk], *, require_changed_line: bool) -> None:
        """청킹 직후. 상관관계용으로 등록하고 개요만 먼저 알린다.

        정적분석 결과는 아직 붙지 않았으므로 상세는 `emit_chunks()` 로 미룬다.
        """
        with self._lock:
            self._chunks = [(c.render_code(), c) for c in chunks]
        self.emit(
            "chunks.planned",
            count=len(chunks),
            require_changed_line=require_changed_line,
            files=sorted({c.path for c in chunks}),
        )

    def emit_chunks(self) -> None:
        """그라운딩까지 끝난 청크 상세를 내보낸다. 여러 번 불러도 한 번만 나간다."""
        with self._lock:
            if self._chunks_emitted:
                return
            self._chunks_emitted = True
            chunks = [c for _, c in self._chunks]

        for chunk in chunks:
            self.emit(
                "chunk.ready",
                chunk_id=chunk.chunk_id,
                path=chunk.path,
                language=chunk.language.value,
                start_line=chunk.start_line,
                end_line=chunk.end_line,
                changed_lines=sorted(chunk.changed_linenos),
                enclosing_symbol=chunk.enclosing_symbol,
                code=clip(chunk.render_code()),
                static_findings=[
                    {
                        "tool": f.tool,
                        "line": f.line,
                        "rule_id": f.rule_id,
                        "message": f.message,
                        "severity": f.severity.value,
                    }
                    for f in chunk.static_findings
                ],
            )

    # -- 상관관계 -----------------------------------------------------------

    def match_chunk(self, prompt: str) -> ReviewChunk | None:
        """프롬프트에 코드가 통째로 들어있는 청크를 찾는다."""
        with self._lock:
            candidates = list(self._chunks)
        for code, chunk in candidates:
            if code and code in prompt:
                return chunk
        return None

    def register_findings(self, chunk: ReviewChunk, items: list[dict[str, Any]]) -> None:
        """생성 응답에서 뽑은 지적을 검증 호출과 이어 붙이려고 기억해 둔다."""
        with self._lock:
            for item in items:
                message = str(item.get("message", "")).strip()
                if message:
                    self._findings.append(
                        (message, {**item, "chunk_id": chunk.chunk_id, "path": chunk.path})
                    )

    def match_finding(self, prompt: str, chunk: ReviewChunk | None = None) -> dict[str, Any] | None:
        """검증 프롬프트가 판정 중인 지적을 찾는다.

        메시지만으로는 부족하다. 같은 결함 패턴이 파일 안 두 곳에 있으면 생성
        모델이 **똑같은 문장**을 두 번 내놓고, 그때 메시지 매칭은 둘 중 아무
        쪽에나 붙는다(개발 중 실제로 그렇게 어긋났다).

        그래서 세 신호를 합산한다. 위치(`path:line`)가 가장 강하다 — 검증
        프롬프트는 어느 위치를 판정하는지 반드시 적게 되어 있고, 그 문자열은
        코드 블록의 `[added @142]` 주석과 형태가 겹치지 않는다.
        """
        with self._lock:
            candidates = list(self._findings)

        scoped = [
            item for _, item in candidates
            if chunk is None or item.get("chunk_id") == chunk.chunk_id
        ]
        best, best_score = _best_match(scoped, prompt)
        if best is not None:
            return best

        # 청크를 못 맞췄거나 그 청크에 후보가 없으면 전체에서 다시 본다.
        if chunk is not None:
            best, best_score = _best_match([item for _, item in candidates], prompt)
        return best


def _best_match(items: list[dict[str, Any]], prompt: str) -> tuple[dict[str, Any] | None, int]:
    """위치 3점, 원문 2점, 룰 ID 1점. 2점 미만은 맞췄다고 보지 않는다."""
    best: dict[str, Any] | None = None
    best_score = 0
    for item in items:
        score = 0
        if f"{item.get('path')}:{item.get('line')}" in prompt:
            score += 3
        message = str(item.get("message") or "")
        if message and message in prompt:
            score += 2
        rule_id = str(item.get("rule_id") or "")
        if rule_id and rule_id in prompt:
            score += 1
        if score > best_score:
            best, best_score = item, score
    return (best, best_score) if best_score >= 2 else (None, 0)


__all__ = ["Event", "MAX_EVENTS", "MAX_TEXT", "Tracer", "clip"]
