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
from crex.viz.api import Context, Request, handle  # noqa: E402
from crex.viz.engine import RunRegistry, TracedPipeline  # noqa: E402
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
        _check("리뷰 대상이 아니다" in failures[0]["error"], f"사유: {failures[0]['error']}")


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
    test_event_cursor_never_replays,
    test_stdlib_server_serves_over_real_http,
    test_asgi_app_answers_without_uvicorn,
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
