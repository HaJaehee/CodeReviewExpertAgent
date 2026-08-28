/*
 * Presentation 계층의 서버 창구.
 *
 * 화면은 `/api/*` 만 안다. 파이프라인도, MCP 도, 파이썬도 모른다.
 *
 * 이벤트는 커서 폴링으로 받는다 (`api.py` 의 설명 참고). 폴링이 실패해도
 * 커서는 그대로라 다음 요청이 놓친 구간을 그대로 이어받는다 — 폐쇄망 프록시가
 * 요청 하나를 끊어도 화면이 어긋나지 않는다.
 */

window.CREX = window.CREX || {};

CREX.client = (function () {
  const POLL_MS = 400;

  async function request(method, path, body) {
    const init = { method: method, headers: {} };
    if (body !== undefined) {
      init.headers['content-type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    const response = await fetch(path, init);
    const text = await response.text();
    let payload;
    try {
      payload = text ? JSON.parse(text) : {};
    } catch (err) {
      throw new Error('서버 응답을 해석할 수 없다: ' + text.slice(0, 200));
    }
    if (!response.ok) {
      throw new Error(payload.error || ('HTTP ' + response.status));
    }
    return payload;
  }

  /*
   * 실행 하나를 끝까지 따라간다.
   *
   * onBatch(events, payload) 는 새 이벤트가 있을 때마다 불린다. 서버가
   * done 을 알리면 마지막 배치까지 넘긴 뒤 종료한다. stop() 으로 중간에
   * 그만둘 수 있다 — 사용자가 기록에서 다른 실행을 눌렀을 때 쓴다.
   */
  function follow(runId, onBatch, onEnd, onError) {
    let cursor = 0;
    let stopped = false;
    let timer = null;

    async function tick() {
      if (stopped) return;
      try {
        const payload = await request('GET', '/api/runs/' + runId + '/events?since=' + cursor);
        if (stopped) return;
        cursor = payload.cursor;
        if (payload.events.length) onBatch(payload.events, payload);
        if (payload.done) {
          onEnd(payload);
          return;
        }
      } catch (err) {
        if (stopped) return;
        onError(err);
        return;
      }
      timer = window.setTimeout(tick, POLL_MS);
    }

    tick();
    return function stop() {
      stopped = true;
      if (timer) window.clearTimeout(timer);
    };
  }

  return {
    config: () => request('GET', '/api/config'),
    setWorkspace: (path) => request('POST', '/api/workspace', { path: path }),
    health: () => request('GET', '/api/health'),
    runs: () => request('GET', '/api/runs'),
    start: (kind, params) => request('POST', '/api/runs', { kind: kind, params: params }),
    cancel: (runId) => request('POST', '/api/runs/' + runId + '/cancel', {}),
    // compile_commands.json 빌드. 상태·로그를 한 응답으로 받는다 (`api.py`).
    compiledb: (since) => request('GET', '/api/compiledb?since=' + (since || 0)),
    startCompiledb: (params) => request('POST', '/api/compiledb', params),
    cancelCompiledb: () => request('POST', '/api/compiledb/cancel', {}),
    browse: (params) => {
      const qs = new URLSearchParams(params || {}).toString();
      return request('GET', '/api/browse' + (qs ? '?' + qs : ''));
    },
    follow: follow,
    POLL_MS: POLL_MS,
  };
})();
