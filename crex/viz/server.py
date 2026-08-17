"""전송 계층 — Application 계층의 바깥쪽 껍질.

    python -m crex.viz --port 18765

uvicorn 이 있으면 ASGI 로, 없으면 stdlib `http.server` 로 뜬다. 두 경로 모두
`api.handle()` 하나만 부르므로 동작은 같다.

## 왜 폴백이 필요한가

`wiki/invariants.md` — 코어는 무의존이고, wheel 하나가 늘 때마다 폐쇄망 반입
보안 검토가 늘어난다. 관제 화면은 편의 기능이지 리뷰 기능이 아니다. 편의 기능
때문에 반입 목록이 길어지면 안 된다. uvicorn 은 이미 반입한 장비에서 쓰고,
그렇지 않은 장비에서도 화면은 떠야 한다.

성능 차이는 이 용도에서 의미가 없다. 사용자 한두 명이 400ms 마다 이벤트를
폴링하는 것이 전부다.

## 127.0.0.1 에 묶는다

기본 바인드는 루프백이다. 관제 화면에는 소스 코드 전문과 프롬프트가 그대로
실린다. `--host 0.0.0.0` 은 그 사실을 알고 쓰라고 일부러 손이 가게 두었다.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from ..cli import force_utf8_output
from ..config import load_config
from ..gitio import resolve_repo_root
from ..rules import load_taxonomy
from .api import Context, Request, Response, error_response, handle
from .engine import RunRegistry

log = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18765

#: 요청 본문 상한. 이 API 로 들어오는 것은 실행 파라미터뿐이라 넉넉하다.
MAX_BODY = 1 << 20


def build_context(
    repo: Path | None = None,
    config_path: Path | None = None,
    out_dir: Path | None = None,
) -> Context:
    """MCP 서버와 같은 환경변수를 읽는다 — 두 곳을 따로 설정하게 두지 않는다."""
    repo_env = os.environ.get("CREX_REPO")
    repo_root = repo or (Path(repo_env).resolve() if repo_env else resolve_repo_root(Path.cwd()))

    config_env = os.environ.get("CREX_CONFIG")
    config = load_config(config_path or (Path(config_env) if config_env else None))

    reports_env = os.environ.get("CREX_REPORTS")
    reports = out_dir or (Path(reports_env) if reports_env else repo_root / "reports")

    taxonomy = load_taxonomy(config.taxonomy_path) if config.taxonomy_path else load_taxonomy()
    return Context(RunRegistry(repo_root, config, reports), taxonomy)


# --------------------------------------------------------------------------
# ASGI (uvicorn)
# --------------------------------------------------------------------------


def build_asgi(ctx: Context):
    """의존성 없이 손으로 쓴 ASGI 앱.

    Starlette/FastAPI 를 쓰지 않은 이유는 하나다 — 라우트가 여섯 개인데 반입
    wheel 을 두세 개 더 늘릴 값어치가 없다. ASGI 자체는 인터페이스일 뿐이다.
    """
    import asyncio

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
            return

        if scope["type"] != "http":
            return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > MAX_BODY:
                return await _send(send, error_response("요청 본문이 너무 크다", 413))
            if not message.get("more_body"):
                break

        request = Request(
            method=scope["method"],
            path=scope["path"],
            query=_query(scope.get("query_string", b"").decode("latin-1")),
            body=bytes(body),
        )
        # 핸들러는 동기다. `/api/health` 는 LLM 엔드포인트를 실제로 부르므로
        # 이벤트 루프에서 직접 돌리면 그동안 폴링이 전부 멈춘다.
        response = await asyncio.to_thread(handle, request, ctx)
        await _send(send, response)

    return app


async def _send(send, response: Response) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": response.status,
            "headers": [
                (k.encode("latin-1"), v.encode("latin-1")) for k, v in response.headers()
            ],
        }
    )
    await send({"type": "http.response.body", "body": response.body})


# --------------------------------------------------------------------------
# stdlib 폴백
# --------------------------------------------------------------------------


def serve_stdlib(ctx: Context, host: str, port: int) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "crex-viz"

        def log_message(self, fmt, *args):  # noqa: A002 - 규약상 이름 고정
            log.debug("%s - %s", self.address_string(), fmt % args)

        def _dispatch(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self._write(error_response("요청 본문이 너무 크다", 413))
                return
            raw = self.rfile.read(length) if length else b""
            split = urlsplit(self.path)
            request = Request(method, split.path, _query(split.query), raw)
            self._write(handle(request, ctx))

        def _write(self, response: Response) -> None:
            self.send_response(response.status)
            for key, value in response.headers():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(response.body)

        def do_GET(self):  # noqa: N802
            self._dispatch("GET")

        def do_POST(self):  # noqa: N802
            self._dispatch("POST")

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _query(raw: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}


# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    force_utf8_output()

    parser = argparse.ArgumentParser(
        prog="python -m crex.viz", description="CREX 리뷰 파이프라인 관제 화면"
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"바인드 주소 (기본 {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"포트 (기본 {DEFAULT_PORT})")
    parser.add_argument("--repo", type=Path, default=None, help="리뷰할 저장소 루트")
    parser.add_argument("--config", type=Path, default=None, help="crex.toml 경로")
    parser.add_argument("--out", type=Path, default=None, help="리포트 저장 위치")
    parser.add_argument("--stdlib", action="store_true", help="uvicorn 이 있어도 stdlib 서버를 쓴다")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    try:
        ctx = build_context(args.repo, args.config, args.out)
    except (OSError, ValueError) as exc:
        print(f"설정 오류: {exc}", file=sys.stderr)
        return 2

    registry = ctx.registry
    log.info("저장소 %s", registry.repo_root)
    log.info("%s", registry.config.describe())
    log.info("리포트 %s", registry.out_dir)

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        log.warning(
            "%s 에 바인드한다. 관제 화면에는 소스 코드와 프롬프트가 그대로 실린다.", args.host
        )

    uvicorn = None if args.stdlib else _import_uvicorn()
    print(f"\n  http://{args.host}:{args.port}\n", file=sys.stderr)

    if uvicorn is None:
        log.info("stdlib http.server 로 뜬다 (uvicorn 없음)")
        serve_stdlib(ctx, args.host, args.port)
        return 0

    log.info("uvicorn %s", getattr(uvicorn, "__version__", "?"))
    uvicorn.run(
        build_asgi(ctx),
        host=args.host,
        port=args.port,
        log_level="debug" if args.verbose else "warning",
        access_log=False,
    )
    return 0


def _import_uvicorn():
    try:
        import uvicorn
    except ImportError:
        return None
    return uvicorn


__all__ = ["DEFAULT_HOST", "DEFAULT_PORT", "build_asgi", "build_context", "main", "serve_stdlib"]
