"""관제 화면 검증.

실제 git 저장소와 가짜 vLLM 으로 파이프라인을 태우고, 그 결과가 **이벤트로
정확히 흘러나오는지**를 본다. 계측이 리뷰 결과를 바꾸지 않는다는 것도 함께 본다 —
관제 때문에 리뷰가 달라지면 관제할 값어치가 없다.

전송 계층은 두 경로 모두 확인한다. stdlib 서버는 실제 소켓으로, ASGI 앱은
uvicorn 없이 asyncio 로 직접 호출해서 — 그래야 폐쇄망 반입 직후에도 이 테스트가
돈다(`wiki/invariants.md`).
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crex.config import ChunkingConfig, Config, GroundingConfig, ReviewConfig  # noqa: E402
from crex.llm import EndpointConfig  # noqa: E402
from crex.pipeline import Pipeline  # noqa: E402
from crex.rules import load_taxonomy  # noqa: E402
from crex.workspace import resolve  # noqa: E402
from crex.viz.api import Context, Request, handle  # noqa: E402
from crex.viz.engine import Run, RunRegistry, TracedPipeline  # noqa: E402
from crex.viz.trace import MAX_TEXT, Tracer, clip  # noqa: E402
from tests.fake_vllm import FakeVLLM  # noqa: E402
from tests.test_mcp import _make_repo  # noqa: E402
from tests.test_pipeline import _git  # noqa: E402

RULE_ID = "cpp.dangling-after-realloc"
MESSAGE = "resize() 이후 raw 포인터가 무효화된다."


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _config(base_url: str) -> Config:
    endpoint = EndpointConfig(base_url=base_url, model="fake", max_retries=1, timeout=10.0)
    return Config(
        generator=endpoint,
        verifier=endpoint,
        review=ReviewConfig(max_workers=2),
        grounding=GroundingConfig(enabled=False),
        chunking=ChunkingConfig(),
    )


def _emit_finding(schema: dict) -> dict:
    items = schema["properties"]["findings"]["items"]["properties"]
    return {
        "findings": [
            {
                "line": items["line"]["enum"][0] if "enum" in items["line"] else 8,
                "rule_id": RULE_ID,
                "severity": "high",
                "message": MESSAGE,
            }
        ]
    }


def _context(repo: Path, base_url: str, out: Path) -> Context:
    return Context(RunRegistry(repo, _config(base_url), out), load_taxonomy())


def _run_to_completion(ctx: Context, kind: str = "staged", params: dict | None = None, timeout: float = 30.0):
    """실행을 띄우고 끝날 때까지 이벤트를 모은다. 화면의 폴링과 같은 경로다."""
    created = handle(
        Request("POST", "/api/runs", body=json.dumps({"kind": kind, "params": params or {}}).encode()),
        ctx,
    )
    _check(created.status == 201, f"실행 생성 실패: {created.body!r}")
    run_id = json.loads(created.body)["id"]

    events: list[dict] = []
    cursor = 0
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = handle(Request("GET", f"/api/runs/{run_id}/events", {"since": str(cursor)}), ctx)
        payload = json.loads(response.body)
        events.extend(payload["events"])
        cursor = payload["cursor"]
        if payload["done"]:
            return run_id, events, payload
        time.sleep(0.02)
    raise AssertionError(f"실행이 {timeout}초 안에 끝나지 않았다 (이벤트 {len(events)}개)")


def _of_type(events: list[dict], type_: str) -> list[dict]:
    return [e["data"] for e in events if e["type"] == type_]


# --------------------------------------------------------------------------
# 계측이 리뷰를 바꾸지 않는다
# --------------------------------------------------------------------------


def test_traced_pipeline_matches_plain_pipeline() -> None:
    """이것이 이 패키지의 유일한 불변식이다 — 관제가 결과를 바꾸면 안 된다."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make_repo(Path(tmp))
        _git(repo, "add", "-A")
        diff = _git(repo, "diff", "--cached")

        with FakeVLLM(on_findings=_emit_finding) as fake:
            config = _config(fake.base_url)
            plain = Pipeline(config).run_diff(diff, repo)
            traced = TracedPipeline(config, Tracer(), threading.Event()).run_diff(diff, repo)

        _check(
            [f.to_dict() for f in plain.kept] == [f.to_dict() for f in traced.kept],
            f"유지된 지적이 다르다:\n{[f.to_dict() for f in plain.kept]}\n"
            f"{[f.to_dict() for f in traced.kept]}",
        )
        _check(
            len(plain.rejected) == len(traced.rejected),
            f"기각 건수가 다르다: {len(plain.rejected)} vs {len(traced.rejected)}",
        )
        _check(plain.chunks_reviewed == traced.chunks_reviewed, "청크 수가 다르다")


def test_stage_events_cover_every_stage() -> None:
    """`_timed` 하나만 감싸서 전 단계를 잡는다는 설계가 실제로 성립하는지."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make_repo(Path(tmp))
        _git(repo, "add", "-A")

        with FakeVLLM(on_findings=_emit_finding) as fake:
            ctx = _context(repo, fake.base_url, Path(tmp) / "reports")
            _, events, _ = _run_to_completion(ctx)

        started = {d["stage"] for d in _of_type(events, "stage.started")}
        finished = {d["stage"] for d in _of_type(events, "stage.finished")}
        # 이 테스트는 grounding 을 꺼두었으므로 ground 단계는 돌지 않는다.
        _check(started == {"chunk", "generate", "filter"}, f"시작한 단계: {sorted(started)}")
        _check(finished == started, f"끝난 단계: {sorted(finished)}")
        _check(
            all(d["seconds"] >= 0 for d in _of_type(events, "stage.finished")),
            "소요 시간이 기록되지 않았다",
        )


# --------------------------------------------------------------------------
# 상관관계
# --------------------------------------------------------------------------


def test_generator_call_is_tied_to_its_chunk() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make_repo(Path(tmp))
        _git(repo, "add", "-A")

        with FakeVLLM(on_findings=_emit_finding) as fake:
            ctx = _context(repo, fake.base_url, Path(tmp) / "reports")
            _, events, _ = _run_to_completion(ctx)

        requests = [d for d in _of_type(events, "llm.request") if d["role"] == "generator"]
        _check(requests, "생성 호출 이벤트가 없다")
        for request in requests:
            _check(request["chunk_id"] is not None, f"청크를 못 맞췄다: {request['path']}")
            _check(request["path"] == "src/buffer.cpp", f"경로: {request['path']}")
            # 화면이 '왜 이 지적이 나왔는가'를 보여주려면 이 셋이 다 있어야 한다.
            _check(request["system"] and request["user"], "프롬프트가 비었다")
            _check("enum" in json.dumps(request["schema"]), "스키마가 실려오지 않았다")

        chunk_ids = {d["chunk_id"] for d in _of_type(events, "chunk.ready")}
        _check(
            {r["chunk_id"] for r in requests} <= chunk_ids,
            "호출이 존재하지 않는 청크를 가리킨다",
        )


def test_verifier_call_is_tied_to_its_finding() -> None:
    """검증 레인에서 '어느 지적을 판정 중인지' 보이지 않으면 화면이 무의미하다."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make_repo(Path(tmp))
        _git(repo, "add", "-A")

        with FakeVLLM(on_findings=_emit_finding) as fake:
            ctx = _context(repo, fake.base_url, Path(tmp) / "reports")
            _, events, _ = _run_to_completion(ctx)

        verifications = [d for d in _of_type(events, "llm.request") if d["role"] == "verifier"]
        _check(verifications, "검증 호출 이벤트가 없다")
        for call in verifications:
            target = call["target"]
            _check(target is not None, "검증 대상 지적을 못 맞췄다")
            _check(target["rule_id"] == RULE_ID, f"룰 ID: {target['rule_id']}")
            _check(target["message"] == MESSAGE, f"지적 원문: {target['message']}")


def test_identical_messages_in_two_places_do_not_cross_wire() -> None:
    """같은 결함이 파일 안 두 곳에 있으면 생성 모델은 똑같은 문장을 두 번 낸다.

    원문만으로 짝을 지으면 검증 레인이 엉뚱한 라인을 가리킨다 — 개발 중 실제로
    그렇게 어긋났다. 위치가 원문보다 강한 신호여야 한다.
    """
    from crex.schema import Language, ReviewChunk

    chunk = ReviewChunk(
        chunk_id="src/a.py#0", path="src/a.py", language=Language.PYTHON,
        start_line=1, end_line=50,
    )
    tracer = Tracer()
    same = "널 검사 없이 역참조한다."
    tracer.register_findings(chunk, [
        {"line": 10, "rule_id": "python.none-deref", "message": same},
        {"line": 42, "rule_id": "python.none-deref", "message": same},
    ])

    prompt = f"## 검증할 지적\n- 위치: src/a.py:42\n- 룰: python.none-deref\n- 내용: {same}\n"
    matched = tracer.match_finding(prompt, chunk)
    _check(matched is not None, "지적을 못 맞췄다")
    _check(matched["line"] == 42, f"라인 10 과 42 가 뒤바뀌었다: {matched['line']}")


def test_unmatched_prompt_does_not_break_the_run() -> None:
    """상관관계 실패는 이벤트의 chunk_id 가 비는 것으로 끝나야 한다."""
    tracer = Tracer()
    _check(tracer.match_chunk("아무 관련 없는 프롬프트") is None, "없는 청크를 맞췄다고 한다")
    _check(tracer.match_finding("아무 관련 없는 프롬프트") is None, "없는 지적을 맞췄다고 한다")


def test_event_cap_never_swallows_the_ending() -> None:
    """상한에 걸려도 run.finished 는 나가야 한다 — 판정 전체가 거기 실린다."""
    from crex.viz.trace import MAX_EVENTS

    tracer = Tracer()
    for _ in range(MAX_EVENTS + 50):
        tracer.emit("llm.request")

    _check(tracer.dropped > 0, "상한이 걸리지 않았다")
    _check(tracer.emit("run.finished", verdicts=[]) is not None, "결말 이벤트가 버려졌다")
    _check(
        tracer.events[-1].type == "run.finished",
        f"마지막 이벤트: {tracer.events[-1].type}",
    )


def test_long_prompt_is_clipped_for_transport() -> None:
    clipped = clip("가" * (MAX_TEXT + 500))
    _check(len(clipped) < MAX_TEXT + 200, f"자르지 않았다: {len(clipped)}자")
    _check("생략" in clipped, "잘렸다는 사실이 본문에 없다")
    _check(clip("짧다") == "짧다", "짧은 텍스트를 건드렸다")


# --------------------------------------------------------------------------
# 결과 전달
# --------------------------------------------------------------------------


def test_run_finished_carries_verdicts_with_reject_reasons() -> None:
    """기각 사유는 필터 튜닝의 유일한 근거 데이터다. 화면까지 와야 한다."""
    def reject(_schema: dict) -> dict:
        return {"verdict": "no", "code_present": False, "reason": "스니펫에 없다"}

    with tempfile.TemporaryDirectory() as tmp:
        repo = _make_repo(Path(tmp))
        _git(repo, "add", "-A")

        with FakeVLLM(on_findings=_emit_finding, on_verdict=reject) as fake:
            ctx = _context(repo, fake.base_url, Path(tmp) / "reports")
            _, events, payload = _run_to_completion(ctx)

        finished = _of_type(events, "run.finished")
        _check(len(finished) == 1, f"run.finished 가 {len(finished)}번")
        verdicts = finished[0]["verdicts"]
        _check(verdicts, "판정이 비었다")
        _check(all(not v["kept"] for v in verdicts), "기각했는데 유지로 나온다")
        _check(
            verdicts[0]["reject_reason"] == "code_not_found",
            f"기각 사유: {verdicts[0]['reject_reason']}",
        )
        _check("스니펫에 없다" in verdicts[0]["reason"], f"사유 원문 누락: {verdicts[0]['reason']}")


def test_agent_summary_is_the_real_mcp_return_value() -> None:
    """화면이 보여주는 요약이 실제 MCP 반환값과 달라지면 관제한 것이 거짓이 된다."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make_repo(Path(tmp))
        _git(repo, "add", "-A")

        with FakeVLLM(on_findings=_emit_finding) as fake:
            ctx = _context(repo, fake.base_url, Path(tmp) / "reports")
            _, events, payload = _run_to_completion(ctx)

        summary = payload["agent_summary"]
        _check("지적 1건" in summary, f"건수 없음:\n{summary}")
        _check("전체 리포트:" in summary, f"리포트 경로 없음:\n{summary}")
        _check("<details>" not in summary, "리포트 전문이 실려 나왔다")
        _check(_of_type(events, "run.finished")[0]["agent_summary"] == summary, "요약이 두 곳에서 다르다")


def test_request_error_becomes_a_failed_run_not_a_crash() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make_repo(Path(tmp))
        with FakeVLLM() as fake:
            ctx = _context(repo, fake.base_url, Path(tmp) / "reports")
            _, events, payload = _run_to_completion(ctx, "file", {"path": "src/notes.txt"})

        _check(payload["run"]["status"] == "failed", f"상태: {payload['run']['status']}")
        failures = _of_type(events, "run.failed")
        _check(failures, "실패 이벤트가 없다")
        # 문장은 service.py 것이지만(해라체), 화면으로 나올 때는 합쇼체다.
        _check("리뷰 대상이 아닙니다" in failures[0]["error"], f"사유: {failures[0]['error']}")


# --------------------------------------------------------------------------
# HTTP 계약
# --------------------------------------------------------------------------


def test_routes_and_static_files() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _context(Path(tmp), "http://127.0.0.1:1/v1", Path(tmp) / "reports")

        index = handle(Request("GET", "/"), ctx)
        _check(index.status == 200, f"루트 상태: {index.status}")
        _check(b"<title>" in index.body, "index.html 이 아니다")
        _check("text/html" in index.content_type, f"콘텐츠 타입: {index.content_type}")

        for name in ("style.css", "store.js", "client.js", "view.js"):
            asset = handle(Request("GET", f"/static/{name}"), ctx)
            _check(asset.status == 200, f"{name} 상태: {asset.status}")
            _check(asset.body, f"{name} 가 비었다")

        config = json.loads(handle(Request("GET", "/api/config"), ctx).body)
        _check(config["taxonomy"]["rules"] > 0, "룰 수가 0")
        _check("api_key" not in json.dumps(config), "설정 응답에 api_key 가 섞였다")

        for path in ("/static/../api.py", "/static/nope.js", "/api/nope"):
            _check(handle(Request("GET", path), ctx).status == 404, f"{path} 가 404 가 아니다")

        _check(handle(Request("GET", "/api/runs/nope"), ctx).status == 404, "없는 실행이 404 가 아니다")
        _check(
            handle(Request("POST", "/api/runs", body=b"{"), ctx).status == 400,
            "깨진 JSON 이 400 이 아니다",
        )
        bad_kind = handle(Request("POST", "/api/runs", body=b'{"kind":"nope"}'), ctx)
        _check(bad_kind.status == 400, f"알 수 없는 종류 상태: {bad_kind.status}")


# --------------------------------------------------------------------------
# 워크스페이스 변경
# --------------------------------------------------------------------------


def _post_workspace(ctx: Context, path: str):
    return handle(
        Request("POST", "/api/workspace", body=json.dumps({"path": path}).encode()), ctx
    )


def test_workspace_switch_updates_everything_at_once() -> None:
    """대상·설정·리포트 위치가 함께 움직여야 한다.

    하나라도 안 따라오면 A 저장소를 리뷰하고 B 저장소에 리포트를 쓰는 상태가 된다.
    """
    with tempfile.TemporaryDirectory() as tmp:
        first = _make_repo(Path(tmp) / "first")
        second = _make_repo(Path(tmp) / "second")
        (second / "crex.json").write_text(
            json.dumps({"llm": {"generator": {"model": "두번째-모델"}}}, ensure_ascii=False),
            encoding="utf-8",
        )

        ctx = _context(first, "http://127.0.0.1:1/v1", Path(tmp) / "reports")
        ctx.registry.workspace = resolve(first, start=Path(tmp), env={})

        before = json.loads(handle(Request("GET", "/api/workspace"), ctx).body)
        _check(before["root"] == str(first.resolve()), f"처음 대상: {before['root']}")

        response = _post_workspace(ctx, str(second))
        _check(response.status == 200, f"상태: {response.status}")

        payload = json.loads(response.body)
        _check(payload["workspace"]["root"] == str(second.resolve()), "대상이 안 바뀌었다")
        _check(ctx.registry.repo_root == second.resolve(), "레지스트리 대상이 안 바뀌었다")
        _check(ctx.registry.out_dir == second.resolve() / "reports", f"리포트: {ctx.registry.out_dir}")
        # 새 저장소 안의 crex.json 을 따라가야 한다.
        _check(
            ctx.registry.config.generator.model == "두번째-모델",
            f"설정이 안 따라왔다: {ctx.registry.config.generator.model}",
        )


def test_workspace_switch_is_refused_while_running() -> None:
    """실행 중 갈아 끼우면 한 리포트에 두 저장소의 결과가 섞인다."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make_repo(Path(tmp))
        other = _make_repo(Path(tmp) / "other")
        _git(repo, "add", "-A")

        ctx = _context(repo, "http://127.0.0.1:1/v1", Path(tmp) / "reports")
        run = Run(id="x", kind="staged", params={}, label="테스트", created_at=0.0)
        ctx.registry._runs[run.id] = run  # noqa: SLF001 - 진행 중 상태를 만드는 가장 단순한 방법
        ctx.registry._order.append(run.id)  # noqa: SLF001

        refused = _post_workspace(ctx, str(other))
        _check(refused.status == 400, f"상태: {refused.status}")
        _check("실행 중" in json.loads(refused.body)["error"], json.loads(refused.body)["error"])
        _check(ctx.registry.repo_root == repo, "거부했는데 대상이 바뀌었다")

        run.status = "done"
        _check(_post_workspace(ctx, str(other)).status == 200, "끝난 뒤에도 거부했다")


def test_workspace_switch_validates_like_startup() -> None:
    """없는 경로는 처음 정할 때와 똑같이 거부한다 — 여기만 느슨하면 우회로가 된다."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make_repo(Path(tmp))
        ctx = _context(repo, "http://127.0.0.1:1/v1", Path(tmp) / "reports")

        bad = _post_workspace(ctx, str(Path(tmp) / "없는폴더"))
        _check(bad.status == 400, f"상태: {bad.status}")
        # workspace.py 는 "없다" 라고 하지만 화면에는 "없습니다" 로 나가야 한다.
        _check("없습니다" in json.loads(bad.body)["error"], json.loads(bad.body)["error"])
        _check(ctx.registry.repo_root == repo, "실패했는데 대상이 바뀌었다")

        empty = _post_workspace(ctx, "   ")
        _check(empty.status == 400, f"빈 경로 상태: {empty.status}")


def test_workspace_switch_blocked_on_remote_bind() -> None:
    """인증이 없는 화면이다. 원격 바인드에서 대상 변경은 임의 디렉터리 열기가 된다."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make_repo(Path(tmp))
        other = _make_repo(Path(tmp) / "other")
        ctx = _context(repo, "http://127.0.0.1:1/v1", Path(tmp) / "reports")
        ctx.workspace_switchable = False

        blocked = _post_workspace(ctx, str(other))
        _check(blocked.status == 403, f"상태: {blocked.status}")
        _check(ctx.registry.repo_root == repo, "막았는데 대상이 바뀌었다")

        # 화면이 버튼을 끌 수 있도록 상태에 실려 나가야 한다.
        state = json.loads(handle(Request("GET", "/api/workspace"), ctx).body)
        _check(state["switchable"] is False, "switchable 이 안 실렸다")


# --------------------------------------------------------------------------
# compile_commands.json 빌드
# --------------------------------------------------------------------------


def _fake_generate(entries: int, *, lines: tuple[str, ...] = ("빌드 시작", "빌드 끝")):
    """`compiledb.generate` 자리에 끼울 가짜.

    MSBuild 도 CMake 도 없는 장비에서 돌아야 한다(`wiki/invariants.md` — 테스트는
    네트워크·LLM·설치 없이 돈다). 진짜 빌드는 여기서 검증할 수 없고, 검증하려는
    것도 그게 아니다 — **만든 뒤에 무엇을 하는가**가 이 경로의 새로운 부분이다.
    """
    from crex.compiledb import Project, Result

    def fake(root, **kwargs):
        on_line = kwargs.get("on_line")
        for line in lines:
            if on_line is not None:
                on_line(line)
        directory = Path(root) / ".crex" / "compiledb"
        directory.mkdir(parents=True, exist_ok=True)
        json_path = directory / "compile_commands.json"
        payload = [
            {"directory": str(root), "file": f"src/f{i}.cpp", "command": "cl.exe"}
            for i in range(entries)
        ]
        json_path.write_text(json.dumps(payload), encoding="utf-8")
        project = Project("msbuild", Path(root) / "App.sln")
        return Result(project, directory, json_path, entries, directory / "msbuild.log")

    return fake


def _build_and_wait(ctx: Context, payload: dict | None = None, timeout: float = 10.0) -> dict:
    started = handle(
        Request("POST", "/api/compiledb", body=json.dumps(payload or {}).encode()), ctx
    )
    _check(started.status == 201, f"빌드 시작 실패: {started.body!r}")

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = json.loads(handle(Request("GET", "/api/compiledb"), ctx).body)
        if state["job"] and state["job"]["status"] != "running":
            return state
        time.sleep(0.02)
    raise AssertionError(f"빌드가 {timeout}초 안에 끝나지 않았다")


def test_compiledb_state_reports_config_project_and_defaults() -> None:
    """화면이 세 번 묻지 않아도 되게, 설정·탐지·기본값이 한 응답에 있어야 한다."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make_repo(Path(tmp))
        (repo / "App.sln").write_text("solution\n", encoding="utf-8")

        ctx = _context(repo, "http://127.0.0.1:1/v1", Path(tmp) / "reports")
        state = json.loads(handle(Request("GET", "/api/compiledb"), ctx).body)

        _check(state["project"]["kind"] == "msbuild", f"탐지 결과: {state['project']}")
        _check(state["project"]["found"].endswith("App.sln"), state["project"]["found"])
        _check(state["status"]["configured"] is None, f"설정: {state['status']}")
        _check(state["defaults"]["target"] == "Rebuild", f"기본값: {state['defaults']}")
        _check(state["job"] is None, "돌지도 않은 빌드가 있다")


def test_compiledb_detection_failure_is_reported_not_raised() -> None:
    """C++ 프로젝트가 아닌 저장소에서도 화면은 떠야 한다. 사유만 실어 보낸다."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make_repo(Path(tmp))
        ctx = _context(repo, "http://127.0.0.1:1/v1", Path(tmp) / "reports")

        response = handle(Request("GET", "/api/compiledb"), ctx)
        _check(response.status == 200, f"상태: {response.status}")
        state = json.loads(response.body)
        _check(state["project"]["found"] is None, f"없는 프로젝트를 찾았다: {state['project']}")
        _check("찾지 못했습니다" in (state["project"]["error"] or ""), str(state["project"]["error"]))


def test_compiledb_success_applies_to_config_and_writes_the_config_file() -> None:
    """만들기만 하고 끝나면 절반이 떨어져 나간다 — 만든 자리가 설정에 꽂혀야 한다."""
    import crex.viz.build as build

    with tempfile.TemporaryDirectory() as tmp:
        repo = _make_repo(Path(tmp))
        (repo / "crex.json").write_text(
            json.dumps({"grounding": {"timeout": 30.0}}), encoding="utf-8"
        )

        ctx = _context(repo, "http://127.0.0.1:1/v1", Path(tmp) / "reports")
        ctx.registry.workspace = resolve(repo, start=Path(tmp), env={})
        ctx.registry.config = ctx.registry.workspace.config

        original = build.generate
        build.generate = _fake_generate(3)
        try:
            state = _build_and_wait(ctx)
        finally:
            build.generate = original

        job = state["job"]
        _check(job["status"] == "done", f"{job['status']}: {job['error']}")
        _check(job["result"]["entries"] == 3, f"엔트리: {job['result']['entries']}")
        # 1) 돌고 있는 설정에 꽂혔다 — 다음 리뷰가 곧바로 쓴다.
        _check(
            ctx.registry.config.grounding.compile_commands_dir == ".crex/compiledb",
            f"설정에 안 꽂혔다: {ctx.registry.config.grounding.compile_commands_dir!r}",
        )
        # 2) crex.json 에도 적혔다 — 다음에 띄울 때도 살아 있다.
        raw = (repo / "crex.json").read_text(encoding="utf-8")
        written = json.loads(raw)
        _check(
            written["grounding"]["compile_commands_dir"] == ".crex/compiledb", raw
        )
        _check(written["grounding"]["timeout"] == 30.0, f"기존 설정이 날아갔다: {raw}")
        _check(job["result"]["saved_to"] == str(repo / "crex.json"), str(job["result"]["saved_to"]))

        # 상태 응답도 같이 따라와야 한다 — 화면이 옛 값을 보여주면 안 된다.
        after = json.loads(handle(Request("GET", "/api/compiledb"), ctx).body)
        _check(after["status"]["entries"] == 3, f"상태: {after['status']}")
        _check(after["status"]["error"] is None, f"상태 오류: {after['status']['error']}")


def test_compiledb_without_save_leaves_the_config_file_alone() -> None:
    """체크박스를 끄면 남의 저장소 파일을 고치지 않는다. 적용은 이 서버 안에서만."""
    import crex.viz.build as build

    with tempfile.TemporaryDirectory() as tmp:
        repo = _make_repo(Path(tmp))
        ctx = _context(repo, "http://127.0.0.1:1/v1", Path(tmp) / "reports")
        ctx.registry.workspace = resolve(repo, start=Path(tmp), env={})
        ctx.registry.config = ctx.registry.workspace.config

        original = build.generate
        build.generate = _fake_generate(2)
        try:
            state = _build_and_wait(ctx, {"save": False})
        finally:
            build.generate = original

        _check(state["job"]["status"] == "done", str(state["job"]["error"]))
        _check(not (repo / "crex.json").exists(), "save=False 인데 설정 파일을 만들었다")
        _check(
            ctx.registry.config.grounding.compile_commands_dir == ".crex/compiledb",
            "이 서버 안에서도 적용이 안 됐다",
        )


def test_compiledb_empty_result_is_not_applied() -> None:
    """빈 DB 를 설정에 꽂으면 '설정했는데 왜 안 되지'가 된다. CLI 와 같은 자리에서 멈춘다."""
    import crex.viz.build as build

    with tempfile.TemporaryDirectory() as tmp:
        repo = _make_repo(Path(tmp))
        ctx = _context(repo, "http://127.0.0.1:1/v1", Path(tmp) / "reports")
        ctx.registry.workspace = resolve(repo, start=Path(tmp), env={})
        ctx.registry.config = ctx.registry.workspace.config

        original = build.generate
        build.generate = _fake_generate(0)
        try:
            state = _build_and_wait(ctx)
        finally:
            build.generate = original

        job = state["job"]
        _check(job["status"] == "failed", f"빈 DB 를 성공으로 봤다: {job['status']}")
        _check("비어 있습니다" in (job["error"] or ""), str(job["error"]))
        _check(
            ctx.registry.config.grounding.compile_commands_dir is None,
            f"빈 DB 가 설정에 꽂혔다: {ctx.registry.config.grounding.compile_commands_dir!r}",
        )
        _check(not (repo / "crex.json").exists(), "빈 DB 를 설정 파일에 적었다")


def test_compiledb_rejects_unknown_and_multiword_options() -> None:
    """설정 파일과 같은 규칙이다. 오타난 값을 조용히 버리면 '설정했는데 안 먹는다'가 된다."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make_repo(Path(tmp))
        ctx = _context(repo, "http://127.0.0.1:1/v1", Path(tmp) / "reports")

        unknown = handle(
            Request("POST", "/api/compiledb", body=b'{"configuraton": "Release"}'), ctx
        )
        _check(unknown.status == 400, f"오타난 키 상태: {unknown.status}")

        # MSBuild 인자로 그대로 들어간다. 공백이 섞이면 스위치를 하나 더 넘기는 모양이 된다.
        spaced = handle(
            Request("POST", "/api/compiledb", body=b'{"configuration": "Debug /p:X=1"}'), ctx
        )
        _check(spaced.status == 400, f"공백 값 상태: {spaced.status}")


def test_compiledb_and_review_never_run_together() -> None:
    """Rebuild 는 산출물을 지웠다 다시 만든다. 그 사이의 DB 로 도는 clang-tidy 는 절반만 본다."""
    from crex.viz.build import BuildParams, new_job

    with tempfile.TemporaryDirectory() as tmp:
        repo = _make_repo(Path(tmp))
        other = _make_repo(Path(tmp) / "other")
        ctx = _context(repo, "http://127.0.0.1:1/v1", Path(tmp) / "reports")

        # 진행 중인 빌드를 만든다 — 스레드를 띄우지 않고 상태만 세운다.
        job = new_job(BuildParams())
        ctx.registry._build = job  # noqa: SLF001 - 진행 중 상태를 만드는 가장 단순한 방법

        refused = handle(Request("POST", "/api/runs", body=b'{"kind":"staged"}'), ctx)
        _check(refused.status == 400, f"빌드 중 리뷰 상태: {refused.status}")
        _check("빌드 중" in json.loads(refused.body)["error"], json.loads(refused.body)["error"])

        moved = _post_workspace(ctx, str(other))
        _check(moved.status == 400, f"빌드 중 대상 변경 상태: {moved.status}")
        _check(ctx.registry.repo_root == repo, "빌드 중인데 대상이 바뀌었다")

        # 반대 방향 — 리뷰가 돌고 있으면 빌드를 받지 않는다.
        job.status = "done"
        run = Run(id="x", kind="staged", params={}, label="테스트", created_at=0.0)
        ctx.registry._runs[run.id] = run  # noqa: SLF001
        ctx.registry._order.append(run.id)  # noqa: SLF001

        blocked = handle(Request("POST", "/api/compiledb", body=b"{}"), ctx)
        _check(blocked.status == 400, f"리뷰 중 빌드 상태: {blocked.status}")
        _check("리뷰 실행 중" in json.loads(blocked.body)["error"], json.loads(blocked.body)["error"])


def test_compiledb_build_blocked_on_remote_bind() -> None:
    """빌드 시작은 서버 장비에서 MSBuild 를 돌리는 일이다. 인증 없는 화면이 원격에서 받을 것이 아니다."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make_repo(Path(tmp))
        ctx = _context(repo, "http://127.0.0.1:1/v1", Path(tmp) / "reports")
        ctx.workspace_switchable = False

        blocked = handle(Request("POST", "/api/compiledb", body=b"{}"), ctx)
        _check(blocked.status == 403, f"상태: {blocked.status}")
        _check(ctx.registry._build is None, "막았는데 빌드가 떴다")  # noqa: SLF001

        # 상태 조회는 계속 되어야 한다 — 화면이 버튼을 끄려면 이 응답이 필요하다.
        state = json.loads(handle(Request("GET", "/api/compiledb"), ctx).body)
        _check(state["switchable"] is False, "switchable 이 안 실렸다")


def test_build_log_cursor_never_replays() -> None:
    """로그도 이벤트와 같다 — 같은 줄을 두 번 주면 화면이 부풀어 오른다."""
    from crex.viz.build import MAX_LOG_LINES, BuildParams, new_job

    job = new_job(BuildParams())
    for i in range(5):
        job.append(f"줄 {i}")

    first = job.tail(0)
    _check(len(first["lines"]) == 5, str(first["lines"]))
    _check(job.tail(first["cursor"])["lines"] == [], "끝난 커서에서 또 나온다")

    job.append("줄 5")
    _check(job.tail(first["cursor"])["lines"] == ["줄 5"], str(job.tail(first["cursor"])["lines"]))

    # 버퍼를 넘기면 밀려 나간 줄은 없는 것이다. 없는 것을 있는 척하지 않는다.
    for i in range(MAX_LOG_LINES + 10):
        job.append(f"넘침 {i}")
    overflowed = job.tail(0)
    _check(len(overflowed["lines"]) == MAX_LOG_LINES, f"{len(overflowed['lines'])}줄")
    _check(overflowed["dropped"] > 0, "밀려 나간 줄 수를 안 알려준다")
    _check(overflowed["cursor"] == 6 + MAX_LOG_LINES + 10, f"커서: {overflowed['cursor']}")


def test_event_cursor_never_replays() -> None:
    """폴링이 같은 이벤트를 두 번 주면 화면의 지표가 부풀어 오른다."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make_repo(Path(tmp))
        _git(repo, "add", "-A")

        with FakeVLLM(on_findings=_emit_finding) as fake:
            ctx = _context(repo, fake.base_url, Path(tmp) / "reports")
            run_id, events, _ = _run_to_completion(ctx)

            seqs = [e["seq"] for e in events]
            _check(seqs == sorted(set(seqs)), "이벤트가 중복되거나 순서가 어긋났다")

            tail = json.loads(
                handle(Request("GET", f"/api/runs/{run_id}/events", {"since": str(seqs[-1])}), ctx).body
            )
            _check(tail["events"] == [], f"끝난 뒤에도 이벤트가 나온다: {len(tail['events'])}개")
            _check(tail["cursor"] == seqs[-1], "커서가 뒤로 갔다")


def test_stdlib_server_serves_over_real_http() -> None:
    """uvicorn 이 없는 장비에서도 화면이 떠야 한다."""
    from crex.viz.server import serve_stdlib

    with tempfile.TemporaryDirectory() as tmp:
        ctx = _context(Path(tmp), "http://127.0.0.1:1/v1", Path(tmp) / "reports")
        port = _free_port()
        thread = threading.Thread(
            target=serve_stdlib, args=(ctx, "127.0.0.1", port), daemon=True
        )
        thread.start()
        _wait_for_port(port)

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as response:
            body = response.read()
        _check(b"<title>" in body, "화면이 내려오지 않았다")

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/config", timeout=5) as response:
            payload = json.loads(response.read())
        _check("kinds" in payload, f"설정 응답이 이상하다: {payload}")


def test_asgi_app_answers_without_uvicorn() -> None:
    """ASGI 앱 자체는 인터페이스일 뿐이다 — uvicorn 없이도 검증된다."""
    import asyncio

    from crex.viz.server import build_asgi

    with tempfile.TemporaryDirectory() as tmp:
        ctx = _context(Path(tmp), "http://127.0.0.1:1/v1", Path(tmp) / "reports")
        app = build_asgi(ctx)
        sent: list[dict] = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        asyncio.run(
            app(
                {"type": "http", "method": "GET", "path": "/api/config", "query_string": b""},
                receive,
                send,
            )
        )

        _check(sent[0]["status"] == 200, f"상태: {sent[0]['status']}")
        headers = {k.decode(): v.decode() for k, v in sent[0]["headers"]}
        _check(headers.get("cache-control") == "no-store", f"캐시 헤더: {headers}")
        _check(b"kinds" in sent[1]["body"], "본문이 이상하다")


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_port(port: int, timeout: float = 5.0) -> None:
    import socket

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.02)
    raise AssertionError(f"{port} 포트가 열리지 않았다")


def test_browse_api_directories_and_files() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        sub = root / "src" / "parser"
        sub.mkdir(parents=True)
        (sub / "token.cpp").write_text("int a = 1;", encoding="utf-8")
        (root / ".git").mkdir()

        ctx = _context(root, "http://127.0.0.1:1/v1", root / "reports")

        # 1. 기본 브라우징 (mode=dir)
        res = handle(Request("GET", "/api/browse", query={"path": str(root), "mode": "dir"}), ctx)
        _check(res.status == 200, f"browse 응답 상태: {res.status}")
        data = json.loads(res.body)
        _check("entries" in data, "entries 누락")
        entry_names = [e["name"] for e in data["entries"]]
        _check("src" in entry_names, f"src 폴더 누락: {entry_names}")

        # 2. 파일 브라우징 (mode=file)
        res_file = handle(Request("GET", "/api/browse", query={"path": str(sub), "mode": "file"}), ctx)
        data_file = json.loads(res_file.body)
        file_entries = [e for e in data_file["entries"] if not e["is_dir"]]
        _check(len(file_entries) == 1, f"파일 수 불일치: {file_entries}")
        _check(file_entries[0]["name"] == "token.cpp", f"파일명 불일치: {file_entries[0]['name']}")
        _check(file_entries[0]["rel_path"] == "src/parser/token.cpp", f"상대 경로 불일치: {file_entries[0]['rel_path']}")

        # 3. 원격 바인드 차단 (workspace_switchable=False)
        ctx_remote = Context(ctx.registry, ctx.taxonomy, workspace_switchable=False)
        remote_res = handle(Request("GET", "/api/browse", query={"scope": "all"}), ctx_remote)
        _check(remote_res.status == 403, f"원격 탐색 403 차단 실패: {remote_res.status}")


TESTS = [
    test_traced_pipeline_matches_plain_pipeline,
    test_stage_events_cover_every_stage,
    test_generator_call_is_tied_to_its_chunk,
    test_verifier_call_is_tied_to_its_finding,
    test_identical_messages_in_two_places_do_not_cross_wire,
    test_unmatched_prompt_does_not_break_the_run,
    test_event_cap_never_swallows_the_ending,
    test_long_prompt_is_clipped_for_transport,
    test_run_finished_carries_verdicts_with_reject_reasons,
    test_agent_summary_is_the_real_mcp_return_value,
    test_request_error_becomes_a_failed_run_not_a_crash,
    test_routes_and_static_files,
    test_workspace_switch_updates_everything_at_once,
    test_workspace_switch_is_refused_while_running,
    test_workspace_switch_validates_like_startup,
    test_workspace_switch_blocked_on_remote_bind,
    test_compiledb_state_reports_config_project_and_defaults,
    test_compiledb_detection_failure_is_reported_not_raised,
    test_compiledb_success_applies_to_config_and_writes_the_config_file,
    test_compiledb_without_save_leaves_the_config_file_alone,
    test_compiledb_empty_result_is_not_applied,
    test_compiledb_rejects_unknown_and_multiword_options,
    test_compiledb_and_review_never_run_together,
    test_compiledb_build_blocked_on_remote_bind,
    test_build_log_cursor_never_replays,
    test_event_cursor_never_replays,
    test_stdlib_server_serves_over_real_http,
    test_asgi_app_answers_without_uvicorn,
    test_browse_api_directories_and_files,
]


def main() -> int:
    from crex.cli import force_utf8_output

    force_utf8_output()
    if shutil.which("git") is None:
        print("SKIP: git 이 없어 관제 테스트를 건너뛴다")
        return 0

    failures = 0
    for test in TESTS:
        try:
            test()
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {test.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"ERROR {test.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"ok   {test.__name__}")
    print(f"\n{len(TESTS) - failures}/{len(TESTS)} 통과")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
