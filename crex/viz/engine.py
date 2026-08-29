"""계측된 파이프라인 실행 — Engine 계층.

리뷰 로직을 다시 쓰지 않는다. `Pipeline` 과 `LLMClient` 를 상속해 관찰 지점
세 곳만 덧댄다.

| 덧댄 곳 | 얻는 것 |
|---|---|
| `Pipeline._timed` | 단계(chunk/ground/generate/filter) 시작·종료와 소요 시간 |
| `Pipeline._review` | 청크 목록 — 상관관계의 기준점 |
| `LLMClient.complete` / `.complete_json` | 생성·검증 호출의 프롬프트·스키마·원문 응답·지연 |

`_review` 를 복제하지 않은 것이 요점이다. 복제했다면 파이프라인이 바뀔 때마다
관제 화면이 조용히 옛 동작을 보여주게 된다. `_timed` 가 파이프라인의 유일한
단계 경계라서 그것 하나만 감싸면 된다.

## 서비스 계층을 그대로 태운다

실행은 `ReviewService` 를 통과한다. MCP 도구가 부르는 것과 **같은 경로**다.
그래서 화면에 "에이전트가 받는 요약"을 실제 문자열 그대로 띄울 수 있다.
관제용 우회로를 따로 만들면 관제한 것이 실제와 달라진다.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Config
from ..llm import LLMClient, LLMError
from ..pipeline import Pipeline
from ..schema import ReviewChunk, ReviewResult
from ..service import ReviewRequestError, ReviewService
from ..workspace import Workspace, WorkspaceError, switch
from .build import BuildJob, BuildParams, execute as execute_build, new_job
from .trace import Event, Tracer, clip

log = logging.getLogger(__name__)

#: 서버 메모리에 남겨두는 실행 개수. 기록의 본거지는 브라우저 localStorage 다.
MAX_RUNS = 12


class RunCancelled(LLMError):
    """사용자가 중단을 눌렀다.

    `LLMError` 를 상속하는 이유: 생성 단계는 청크별로, 검증 단계는 건별로 이미
    예외를 흡수하도록 되어 있다. 같은 계열로 던지면 중단이 곧 '보수적 기각'으로
    귀결되고, 중단된 실행도 형태가 온전한 결과를 남긴다.
    """


# --------------------------------------------------------------------------
# LLM 클라이언트 계측
# --------------------------------------------------------------------------


class TracedLLMClient(LLMClient):
    """호출 하나를 요청·응답 이벤트 한 쌍으로 중계한다."""

    def __init__(
        self, inner: LLMClient, role: str, tracer: Tracer, cancel: threading.Event
    ) -> None:
        super().__init__(inner.config)
        self.role = role  # "generator" | "verifier"
        self.tracer = tracer
        self.cancel = cancel
        #: `complete_json` 이 내부에서 부른 `complete` 의 원문을 되받는 통로.
        #: 같은 스레드에서만 오가므로 thread-local 이면 충분하다.
        self._local = threading.local()

    def complete(self, system: str, user: str, **kwargs: Any) -> str:
        raw = super().complete(system, user, **kwargs)
        self._local.raw = raw
        return raw

    def complete_json(
        self,
        system: str,
        user: str,
        json_schema: dict,
        *,
        schema_name: str = "response",
        max_output_tokens: int | None = None,
    ) -> dict:
        # 중단은 요청을 내보내기 *전에* 본다. 일어나지도 않은 호출이 화면에
        # 카드로 남으면 나중에 로그를 읽는 사람이 헷갈린다.
        if self.cancel.is_set():
            raise RunCancelled("사용자가 실행을 중단했습니다")

        call_id = self.tracer.next_call_id()
        chunk = self.tracer.match_chunk(user)
        finding = self.tracer.match_finding(user, chunk) if self.role == "verifier" else None

        self.tracer.emit(
            "llm.request",
            call_id=call_id,
            role=self.role,
            model=self.config.model,
            base_url=self.config.base_url,
            schema_name=schema_name,
            chunk_id=chunk.chunk_id if chunk else None,
            path=chunk.path if chunk else None,
            target=_target_of(finding),
            system=clip(system),
            user=clip(user),
            schema=json_schema,
        )

        self._local.raw = None
        started = time.perf_counter()
        try:
            parsed = super().complete_json(
                system, user, json_schema,
                schema_name=schema_name, max_output_tokens=max_output_tokens,
            )
        except Exception as exc:  # noqa: BLE001 - 그대로 다시 던진다. 기록만 남긴다.
            self.tracer.emit(
                "llm.error",
                call_id=call_id,
                role=self.role,
                latency_ms=_ms(started),
                chunk_id=chunk.chunk_id if chunk else None,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise

        self.tracer.emit(
            "llm.response",
            call_id=call_id,
            role=self.role,
            latency_ms=_ms(started),
            chunk_id=chunk.chunk_id if chunk else None,
            path=chunk.path if chunk else None,
            target=_target_of(finding),
            raw=clip(getattr(self._local, "raw", None) or ""),
            parsed=parsed,
        )

        if self.role == "generator" and chunk is not None:
            items = parsed.get("findings") or []
            if isinstance(items, list):
                self.tracer.register_findings(chunk, [i for i in items if isinstance(i, dict)])

        return parsed


def _target_of(finding: dict[str, Any] | None) -> dict[str, Any] | None:
    if not finding:
        return None
    return {
        "chunk_id": finding.get("chunk_id"),
        "path": finding.get("path"),
        "line": finding.get("line"),
        "rule_id": finding.get("rule_id"),
        "message": finding.get("message"),
    }


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


# --------------------------------------------------------------------------
# 파이프라인 계측
# --------------------------------------------------------------------------


class TracedPipeline(Pipeline):
    """단계 경계와 청크 목록을 이벤트로 흘린다. 리뷰 동작은 건드리지 않는다."""

    def __init__(
        self, config: Config, tracer: Tracer, cancel: threading.Event
    ) -> None:
        super().__init__(config)
        self.tracer = tracer
        self.cancel = cancel
        self.generator = TracedLLMClient(self.generator, "generator", tracer, cancel)
        self.verifier = TracedLLMClient(self.verifier, "verifier", tracer, cancel)

    @contextmanager
    def _timed(self, result: ReviewResult, name: str):
        self.tracer.stage_started(name)
        try:
            with super()._timed(result, name):
                yield
        finally:
            self.tracer.stage_finished(name, result.timings.get(name))
            if name == "ground":
                # 정적분석 결과가 청크에 붙은 뒤라야 상세가 완전해진다.
                self.tracer.emit_chunks()

    def _review(
        self,
        chunks: list[ReviewChunk],
        repo_root: Path,
        result: ReviewResult,
        *,
        require_changed_line: bool,
    ) -> ReviewResult:
        self.tracer.plan(chunks, require_changed_line=require_changed_line)
        if not self.config.grounding.enabled:
            self.tracer.emit_chunks()
        out = super()._review(
            chunks, repo_root, result, require_changed_line=require_changed_line
        )
        self.tracer.result = out
        return out


# --------------------------------------------------------------------------
# 실행 레지스트리
# --------------------------------------------------------------------------


#: 화면이 고를 수 있는 리뷰 종류 → `ReviewService` 메서드.
KINDS = ("staged", "working_tree", "diff", "file", "directory")


@dataclass
class Run:
    id: str
    kind: str
    params: dict[str, Any]
    label: str
    created_at: float
    status: str = "running"  # running | done | failed | cancelled
    error: str | None = None
    agent_summary: str | None = None
    tracer: Tracer = field(default_factory=Tracer)
    cancel: threading.Event = field(default_factory=threading.Event)

    def head(self) -> dict[str, Any]:
        """이벤트를 뺀 요약. 목록과 폴링 응답의 머리말로 쓴다."""
        return {
            "id": self.id,
            "kind": self.kind,
            "params": self.params,
            "label": self.label,
            "created_at": self.created_at,
            "status": self.status,
            "error": self.error,
            "dropped_events": self.tracer.dropped,
        }


class RunRegistry:
    """실행을 띄우고, 이벤트를 커서로 나눠준다.

    서버는 진행 중인 실행만 알면 된다. 오래된 실행은 밀려 나가고, 사용자의
    기록은 브라우저 localStorage 에 남는다.
    """

    def __init__(
        self,
        repo_root: Path,
        config: Config,
        out_dir: Path,
        *,
        max_runs: int = MAX_RUNS,
        workspace: Workspace | None = None,
    ) -> None:
        self.repo_root = repo_root
        self.config = config
        self.out_dir = out_dir
        self.max_runs = max_runs
        #: 대상을 바꾸려면 어디서 왔는지 알아야 한다 (`retarget`).
        self.workspace = workspace
        self._lock = threading.Lock()
        self._runs: dict[str, Run] = {}
        self._order: list[str] = []
        #: 진행 중이거나 마지막으로 끝난 compile_commands.json 빌드. 한 번에 하나다.
        self._build: BuildJob | None = None

    # -- 대상 ---------------------------------------------------------------

    def retarget(self, path: str) -> Workspace:
        """리뷰 대상 저장소를 바꾼다.

        **진행 중인 실행이 있으면 거부한다.** `_execute` 는 실행 중에
        `self.repo_root` 와 `self.config` 를 읽는다. 도중에 갈아 끼우면 청크는
        이쪽 저장소, 정적분석은 저쪽 저장소를 본 결과가 한 리포트에 섞인다.
        `start()` 도 같은 락을 쥐고 실행을 등록하므로 그 사이로 끼어들 틈은 없다.
        """
        with self._lock:
            running = [r.id for r in self._runs.values() if r.status == "running"]
            if running:
                raise ReviewRequestError(
                    f"실행 중({running[0]})에는 워크스페이스를 바꿀 수 없습니다. "
                    f"끝나기를 기다리거나 중단하세요."
                )
            # 빌드도 같다. 빌드는 지금 저장소를 상대로 돌고 있고, 끝나면 그 결과를
            # `self.config` 에 꽂는다 — 도중에 대상을 갈아 끼우면 A 저장소를 빌드한
            # 값이 B 저장소의 설정으로 들어간다.
            if self._build is not None and self._build.status == "running":
                raise ReviewRequestError(
                    "compile_commands.json 빌드 중에는 워크스페이스를 바꿀 수 없습니다. "
                    "끝나기를 기다리거나 중단하세요."
                )
            try:
                changed = switch(self.workspace, path)
            except WorkspaceError as exc:
                # 사용자가 고칠 수 있는 문제. 화면에 그대로 띄운다.
                raise ReviewRequestError(str(exc)) from exc
            self.workspace = changed
            self.repo_root = changed.root
            self.config = changed.config
            self.out_dir = changed.reports
        log.info("워크스페이스 변경: %s", changed.describe())
        return changed

    # -- 조회 ---------------------------------------------------------------

    def get(self, run_id: str) -> Run | None:
        with self._lock:
            return self._runs.get(run_id)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._runs[i].head() for i in reversed(self._order)]

    def events(self, run_id: str, since: int) -> dict[str, Any] | None:
        run = self.get(run_id)
        if run is None:
            return None
        events: list[Event] = run.tracer.since(since)
        return {
            "run": run.head(),
            "events": [e.to_dict() for e in events],
            "cursor": events[-1].seq if events else since,
            "done": run.status != "running",
            "agent_summary": run.agent_summary,
        }

    def cancel(self, run_id: str) -> bool:
        run = self.get(run_id)
        if run is None or run.status != "running":
            return False
        run.cancel.set()
        run.tracer.emit("run.cancelling")
        return True

    # -- 실행 ---------------------------------------------------------------

    def start(self, kind: str, params: dict[str, Any]) -> Run:
        if kind not in KINDS:
            raise ReviewRequestError(f"알 수 없는 리뷰 종류입니다: {kind!r}. 가능: {list(KINDS)}")

        # 빌드 중에는 리뷰를 받지 않는다. Rebuild 는 산출물을 지웠다 다시 만들고,
        # 그 사이의 compile_commands.json 은 반쪽이다. 반쪽짜리 DB 로 도는
        # clang-tidy 는 조용히 절반만 본다 — 없는 것보다 나쁘다.
        with self._lock:
            if self._build is not None and self._build.status == "running":
                raise ReviewRequestError(
                    "compile_commands.json 빌드 중에는 리뷰를 시작할 수 없습니다. "
                    "끝나기를 기다리거나 빌드를 중단하세요."
                )

        run = Run(
            id=uuid.uuid4().hex[:12],
            kind=kind,
            params=params,
            label=_label(kind, params),
            created_at=time.time(),
        )
        with self._lock:
            self._runs[run.id] = run
            self._order.append(run.id)
            while len(self._order) > self.max_runs:
                self._runs.pop(self._order.pop(0), None)

        threading.Thread(target=self._execute, args=(run,), name=f"crex-run-{run.id}", daemon=True).start()
        return run

    def _execute(self, run: Run) -> None:
        run.tracer.emit(
            "run.started",
            kind=run.kind,
            params=run.params,
            label=run.label,
            repo_root=str(self.repo_root),
            config=describe_config(self.config),
        )

        service = ReviewService(
            self.repo_root,
            self.config,
            out_dir=self.out_dir,
            pipeline_factory=lambda cfg: TracedPipeline(cfg, run.tracer, run.cancel),
        )

        try:
            summary = _dispatch(service, run.kind, run.params)
        except ReviewRequestError as exc:
            run.status = "failed"
            run.error = str(exc)
            run.tracer.emit("run.failed", error=run.error, kind="request")
            return
        except Exception as exc:  # noqa: BLE001 - 실행 스레드에서 새어 나가면 안 된다
            log.exception("실행 %s 실패", run.id)
            run.status = "failed"
            run.error = f"{type(exc).__name__}: {exc}"
            run.tracer.emit("run.failed", error=run.error, kind="internal")
            return

        run.agent_summary = summary
        run.status = "cancelled" if run.cancel.is_set() else "done"

        result = run.tracer.result
        run.tracer.emit(
            "run.finished",
            status=run.status,
            agent_summary=summary,
            result=result.to_dict() if result else None,
            verdicts=_verdicts(result),
        )

    # -- compile_commands.json ---------------------------------------------

    def build_state(self, since: int = 0) -> dict[str, Any]:
        """진행 중이거나 마지막으로 끝난 빌드의 상태와 로그 꼬리."""
        job = self._build
        if job is None:
            return {"job": None, "lines": [], "cursor": 0, "dropped": 0}
        return {"job": job.head(), **job.tail(since)}

    def start_build(self, params: BuildParams) -> BuildJob:
        """compile_commands.json 빌드를 띄운다. 한 번에 하나만 돈다.

        리뷰와 같은 락을 쥐고 등록한다 — `start()` 는 빌드 중인지 보고, 여기서는
        리뷰 중인지 본다. 둘이 같은 저장소를 동시에 만지지 못하게 하는 것이 요점이다.
        """
        with self._lock:
            if self._build is not None and self._build.status == "running":
                raise ReviewRequestError(
                    f"이미 빌드 중입니다({self._build.id}). 끝나기를 기다리거나 중단하세요."
                )
            running = [r.id for r in self._runs.values() if r.status == "running"]
            if running:
                raise ReviewRequestError(
                    f"리뷰 실행 중({running[0]})에는 빌드할 수 없습니다. "
                    f"끝나기를 기다리거나 중단하세요."
                )
            job = new_job(params)
            self._build = job
            root, config, workspace = self.repo_root, self.config, self.workspace

        threading.Thread(
            target=execute_build,
            args=(job, root, config, workspace),
            name=f"crex-build-{job.id}",
            daemon=True,
        ).start()
        return job

    def cancel_build(self) -> bool:
        job = self._build
        if job is None or job.status != "running":
            return False
        job.cancel.set()
        job.append("… 중단을 요청했습니다. 진행 중인 빌드 단계가 끝나면 멈춥니다.")
        return True

    # ------------------------------------------------------------------


def _dispatch(service: ReviewService, kind: str, params: dict[str, Any]) -> str:
    paths = params.get("paths") or None
    if kind == "staged":
        return service.review_staged(paths)
    if kind == "working_tree":
        return service.review_working_tree(paths)
    if kind == "diff":
        return service.review_diff(
            str(params.get("from_ref", "")),
            str(params.get("to_ref") or "HEAD"),
            paths,
            use_merge_base=bool(params.get("use_merge_base", True)),
        )
    if kind == "file":
        return service.review_file(str(params.get("path", "")))
    return service.review_directory(
        str(params.get("path", "")), bool(params.get("recursive", True))
    )


def _label(kind: str, params: dict[str, Any]) -> str:
    if kind == "staged":
        return "스테이징된 변경"
    if kind == "working_tree":
        return "커밋 전 전체 변경"
    if kind == "diff":
        return f"{params.get('from_ref', '?')} → {params.get('to_ref') or 'HEAD'}"
    return str(params.get("path", "?"))


def _verdicts(result: ReviewResult | None) -> list[dict[str, Any]]:
    """유지·기각을 한 목록으로 합친다.

    화면은 '무엇이 살아남았는가'보다 '무엇이 왜 죽었는가'를 봐야 한다. 기각
    사유는 필터 튜닝의 유일한 근거 데이터이므로 통째로 넘긴다.
    """
    if result is None:
        return []

    rows = [
        {
            "kept": True,
            "reason": "",
            "reject_reason": None,
            "short_circuited": False,
            **f.to_dict(),
        }
        for f in result.kept
    ]
    for verdict in result.rejected:
        rows.append(
            {
                "kept": False,
                "reason": verdict.reason,
                "reject_reason": verdict.reject_reason.value if verdict.reject_reason else None,
                "short_circuited": verdict.short_circuited,
                **verdict.finding.to_dict(),
            }
        )
    return rows


def describe_config(config: Config) -> dict[str, Any]:
    """화면 머리말에 띄울 설정 요약. 비밀은 넣지 않는다 (api_key 는 제외)."""
    return {
        "mode": config.review.mode,
        "min_severity": config.review.min_severity,
        "max_workers": config.review.max_workers,
        "max_findings_per_chunk": config.review.max_findings_per_chunk,
        "require_changed_line": config.review.require_changed_line,
        "grounding_enabled": config.grounding.enabled,
        "analyzers": config.grounding.analyzers,
        # C++ 은 이 값이 없으면 clang-tidy 가 헤더를 못 찾는다. 화면 머리말에서
        # 바로 보여야 "왜 지적이 안 나오지"를 설정에서 확인할 수 있다.
        "compile_commands_dir": config.grounding.compile_commands_dir,
        "chunking": {
            "expansion_limit": config.chunking.expansion_limit,
            "expansion_truncate": config.chunking.expansion_truncate,
            "absolute_max_lines": config.chunking.absolute_max_lines,
            "on_mismatch": config.chunking.on_mismatch,
        },
        "generator": _endpoint(config.generator),
        "verifier": _endpoint(config.verifier),
        "source": str(config.source) if config.source else None,
    }


def _endpoint(endpoint: Any) -> dict[str, Any]:
    return {
        "model": endpoint.model,
        "base_url": endpoint.base_url,
        "temperature": endpoint.temperature,
        "max_input_tokens": endpoint.max_input_tokens,
        "max_output_tokens": endpoint.max_output_tokens,
        "structured_output_mode": endpoint.structured_output_mode,
    }


__all__ = [
    "KINDS",
    "BuildJob",
    "BuildParams",
    "MAX_RUNS",
    "Run",
    "RunCancelled",
    "RunRegistry",
    "TracedLLMClient",
    "TracedPipeline",
    "describe_config",
]
