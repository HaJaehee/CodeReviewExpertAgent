"""HTTP 계약 — Application 계층.

전송 방식을 모른다. `Request` 를 받아 `Response` 를 돌려주는 순수 함수 하나가
전부이고, uvicorn(ASGI)과 stdlib `http.server` 가 각자 자기 방식으로 그것을
호출한다(`server.py`). 덕분에 라우팅과 JSON 계약은 uvicorn 없이도 테스트된다 —
MCP 에서 `service.py` 와 `mcp.py` 를 나눈 것과 같은 이유다.

## 폴링을 쓴다

이벤트는 `GET /api/runs/{id}/events?since=<커서>` 로 가져간다. SSE 나 WebSocket
이 더 그럴듯해 보이지만, 폐쇄망의 사내 프록시·역프록시가 롱리브 연결을 어떻게
다룰지가 장비마다 다르다. 커서 폴링은 어디서든 똑같이 동작하고, 끊겨도 다음
요청이 이어받는다. 400ms 간격이면 사람 눈에는 실시간이다.

## 화면 파일

`web/` 안의 파일만 이름으로 지정해 내보낸다. 경로를 받아 파일로 바꾸지 않으므로
경로 탈출이 성립하지 않는다.
"""

from __future__ import annotations

import json
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..rules import Taxonomy
from ..service import ReviewRequestError
from .engine import KINDS, RunRegistry, describe_config

WEB_ROOT = Path(__file__).resolve().parent / "web"

#: 내보낼 화면 파일. 목록에 없는 이름은 404 다.
STATIC_FILES = ("index.html", "style.css", "store.js", "client.js", "view.js")


@dataclass
class Request:
    method: str
    path: str
    query: dict[str, str] = field(default_factory=dict)
    body: bytes = b""

    def json(self) -> dict[str, Any]:
        if not self.body:
            return {}
        try:
            data = json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReviewRequestError(f"요청 본문이 JSON 이 아니다: {exc}") from exc
        if not isinstance(data, dict):
            raise ReviewRequestError("요청 본문은 JSON 객체여야 한다")
        return data


@dataclass
class Response:
    status: int
    body: bytes
    content_type: str = "application/json; charset=utf-8"

    def headers(self) -> list[tuple[str, str]]:
        return [
            ("content-type", self.content_type),
            ("content-length", str(len(self.body))),
            # 관제 화면은 항상 지금 상태를 보여줘야 한다. 프록시가 끼어 있는
            # 폐쇄망에서 이벤트 응답이 캐시되면 화면이 멈춘 것처럼 보인다.
            ("cache-control", "no-store"),
        ]


def json_response(payload: Any, status: int = 200) -> Response:
    return Response(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def error_response(message: str, status: int = 400) -> Response:
    return json_response({"error": message}, status)


@dataclass
class Context:
    """Application 계층이 아래 계층을 보는 창구."""

    registry: RunRegistry
    taxonomy: Taxonomy
    version: str = "0.1.0"
    #: 워크스페이스 변경을 받아줄지. 루프백이 아닌 주소에 바인드하면 서버가 끈다 —
    #: 화면에는 인증이 없고, 대상 변경은 "이 저장소"를 "아무 디렉터리"로 넓힌다.
    workspace_switchable: bool = True


# --------------------------------------------------------------------------


def handle(request: Request, ctx: Context) -> Response:
    """라우팅 한 곳. 전송 계층은 이것만 부른다."""
    path = request.path.rstrip("/") or "/"

    try:
        exact = _EXACT.get(path)
        if exact is not None:
            return exact(request, ctx, "")
        for prefix, handler in _PREFIX:
            if path.startswith(prefix):
                return handler(request, ctx, path[len(prefix) :])
        return error_response(f"없는 경로: {request.path}", 404)
    except ReviewRequestError as exc:
        # 사용자가 고칠 수 있는 문제. 화면에 그대로 띄운다.
        return error_response(str(exc), 400)
    except Exception as exc:  # noqa: BLE001 - 전송 계층까지 새어 나가면 서버가 죽는다
        return error_response(f"{type(exc).__name__}: {exc}", 500)


# -- 화면 -------------------------------------------------------------------


def _index(request: Request, ctx: Context, rest: str) -> Response:
    return _static_file("index.html")


def _static(request: Request, ctx: Context, rest: str) -> Response:
    if rest not in STATIC_FILES:
        return error_response(f"없는 파일: {rest}", 404)
    return _static_file(rest)


def _static_file(name: str) -> Response:
    try:
        body = (WEB_ROOT / name).read_bytes()
    except OSError as exc:
        return error_response(f"화면 파일을 읽을 수 없다 ({name}): {exc}", 500)
    guessed = mimetypes.guess_type(name)[0] or "application/octet-stream"
    return Response(200, body, f"{guessed}; charset=utf-8")


# -- API --------------------------------------------------------------------


def _config(request: Request, ctx: Context, rest: str) -> Response:
    registry = ctx.registry
    return json_response(
        {
            "version": ctx.version,
            "repo_root": str(registry.repo_root),
            "workspace": _workspace_state(ctx),
            "out_dir": str(registry.out_dir),
            "kinds": list(KINDS),
            "config": describe_config(registry.config),
            "taxonomy": {
                "rules": len(ctx.taxonomy.rules),
                "by_language": _rules_by_language(ctx.taxonomy),
            },
        }
    )


def _workspace_state(ctx: Context) -> dict[str, Any]:
    workspace = ctx.registry.workspace
    return {
        "root": str(ctx.registry.repo_root),
        "origin": workspace.origin if workspace else "현재 디렉터리",
        "is_git": workspace.is_git if workspace else True,
        "switchable": ctx.workspace_switchable,
    }


def _workspace(request: Request, ctx: Context, rest: str) -> Response:
    """`GET /api/workspace` — 현재 대상. `POST` — 대상 변경.

    변경은 화면에서 오는 것이라도 서버 상태를 바꾸는 일이다. 실행 중이면
    레지스트리가 거부하고(409 가 아니라 400 으로 내려간다 — 사용자가 읽고 조치할
    문제다), 루프백이 아닌 주소에 바인드된 서버는 아예 받지 않는다.
    """
    if request.method == "GET":
        return json_response(_workspace_state(ctx))
    if request.method != "POST":
        return error_response(f"{request.method} 는 지원하지 않는다", 405)

    if not ctx.workspace_switchable:
        return error_response(
            "이 서버는 루프백이 아닌 주소에 바인드되어 있어 워크스페이스를 "
            "바꿀 수 없다. 대상 변경은 임의의 디렉터리를 열 수 있게 하는 일이라 "
            "원격에서는 받지 않는다. --workspace 로 다시 띄우라.",
            403,
        )

    path = str(request.json().get("path", "")).strip()
    if not path:
        raise ReviewRequestError("path 가 필요하다")

    ctx.registry.retarget(path)
    # 화면이 한 번 더 물어보지 않게 새 상태를 통째로 돌려준다.
    return _config(request, ctx, "")


def _rules_by_language(taxonomy: Taxonomy) -> dict[str, int]:
    counts: dict[str, int] = {}
    for rule in taxonomy.rules:
        counts[rule.language] = counts.get(rule.language, 0) + 1
    return dict(sorted(counts.items()))


def _health(request: Request, ctx: Context, rest: str) -> Response:
    """두 엔드포인트에 실제로 한 번씩 말을 걸어본다.

    폐쇄망에서 가장 흔한 실패는 '엔드포인트가 안 떠 있다'와 '모델 이름이 다르다'
    두 가지다. 리뷰를 돌려보고 나서 알게 되면 시간이 아깝다.
    """
    from ..llm import LLMClient

    config = ctx.registry.config
    checks = {}
    for role, endpoint in (("generator", config.generator), ("verifier", config.verifier)):
        ok, detail = LLMClient(endpoint).health()
        checks[role] = {
            "ok": ok,
            "detail": detail,
            "model": endpoint.model,
            "base_url": endpoint.base_url,
        }
    return json_response({"checks": checks, "ok": all(c["ok"] for c in checks.values())})


def _runs(request: Request, ctx: Context, rest: str) -> Response:
    if request.method == "POST":
        payload = request.json()
        kind = str(payload.get("kind", "staged"))
        params = payload.get("params") or {}
        if not isinstance(params, dict):
            raise ReviewRequestError("params 는 JSON 객체여야 한다")
        run = ctx.registry.start(kind, params)
        return json_response(run.head(), 201)
    if request.method != "GET":
        return error_response(f"{request.method} 는 지원하지 않는다", 405)
    return json_response({"runs": ctx.registry.list()})


def _run_detail(request: Request, ctx: Context, rest: str) -> Response:
    """`/api/runs/<id>` 와 `/api/runs/<id>/events`, `/api/runs/<id>/cancel`."""
    run_id, _, action = rest.partition("/")
    if not run_id:
        return _runs(request, ctx, "")

    if action == "events":
        since = _int(request.query.get("since"), 0)
        payload = ctx.registry.events(run_id, since)
        if payload is None:
            return error_response(f"없는 실행: {run_id}", 404)
        return json_response(payload)

    if action == "cancel":
        if request.method != "POST":
            return error_response("cancel 은 POST 다", 405)
        if not ctx.registry.cancel(run_id):
            return error_response("이미 끝났거나 없는 실행이다", 409)
        return json_response({"cancelled": run_id})

    if action:
        return error_response(f"없는 경로: {request.path}", 404)

    run = ctx.registry.get(run_id)
    if run is None:
        return error_response(f"없는 실행: {run_id}", 404)
    return json_response(run.head())


def _int(raw: str | None, default: int) -> int:
    try:
        return int(raw) if raw is not None else default
    except ValueError:
        return default


#: 정확히 일치해야 하는 경로. 접두 표에 앞서 본다.
_EXACT: dict[str, Callable[[Request, "Context", str], Response]] = {
    "/": _index,
    "/index.html": _index,
    "/api/config": _config,
    "/api/health": _health,
    "/api/workspace": _workspace,
    "/api/runs": _runs,
}

#: 뒤에 붙는 부분을 핸들러에 넘기는 접두 경로.
_PREFIX: tuple[tuple[str, Callable[[Request, "Context", str], Response]], ...] = (
    ("/api/runs/", _run_detail),
    ("/static/", _static),
)


__all__ = [
    "Context",
    "Request",
    "Response",
    "STATIC_FILES",
    "WEB_ROOT",
    "error_response",
    "handle",
    "json_response",
]
