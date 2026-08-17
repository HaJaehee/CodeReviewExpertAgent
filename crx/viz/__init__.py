"""crx 파이프라인 관제 — 생성 모델과 검증 모델이 무슨 일을 하는지 웹으로 본다.

    python -m crx.viz            # http://127.0.0.1:8765

MCP 서버는 에이전트에게 **압축 요약**만 돌려준다(`crx/service.py` 참고). 컨텍스트를
아끼려는 의도된 설계지만, 그 대가로 파이프라인 안에서 벌어지는 일이 통째로
보이지 않는다. 어떤 청크가 만들어졌는지, 생성 모델이 무엇을 뱉었는지, 검증
모델이 왜 기각했는지 — 프롬프트를 다듬으려면 정확히 그것들을 봐야 한다.

이 패키지는 그 안쪽을 그대로 중계한다. 리뷰 로직은 한 줄도 다시 쓰지 않는다.

## 3계층

| 계층 | 모듈 | 책임 |
|---|---|---|
| Presentation | `web/` (`index.html`, `style.css`, `store.js`, `client.js`, `view.js`) | 화면. 실행 기록은 브라우저 **localStorage** 에 남는다 |
| Application | `api.py`, `server.py` | HTTP 계약과 라우팅. 전송(uvicorn/stdlib)은 `server.py` 가 갈아끼운다 |
| Engine | `engine.py`, `trace.py` | 계측된 파이프라인 실행. `crx.pipeline.Pipeline` 을 상속만 한다 |

각 계층은 아래만 안다. `api.py` 는 `Pipeline` 을 모르고, `engine.py` 는 HTTP 를
모르며, 화면은 `/api/*` 만 안다.

## 서버에 DB 가 없다

실행 기록은 브라우저 localStorage 에 쌓인다. 서버는 진행 중인 실행의 이벤트를
메모리 링버퍼로만 들고 있다가 버린다. 관제 화면 하나 때문에 폐쇄망 장비에 DB 를
하나 더 세우고 그 백업·권한·반입 승인을 떠안을 이유가 없다.

## 의존성

uvicorn 이 있으면 쓰고, 없으면 stdlib `http.server` 로 뜬다. 코어 무의존 원칙
(`wiki/invariants.md`)은 그대로다 — 관제 화면 때문에 반입 wheel 이 늘어나서는
안 된다.
"""

from __future__ import annotations

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    """`python -m crx.viz` 진입점. 무거운 import 를 미루려고 여기서 넘긴다."""
    from .server import main as _main

    return _main(argv)
