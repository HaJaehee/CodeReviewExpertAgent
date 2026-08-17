/*
 * Presentation 계층의 저장소 — localStorage.
 *
 * 서버에는 DB 가 없다. 진행 중인 실행의 이벤트만 메모리에 있다가 사라지고,
 * 기록은 전부 여기에 남는다. 관제 화면 하나 때문에 폐쇄망 장비에 DB 를 세우고
 * 그 백업·권한·반입 승인을 떠안을 이유가 없다.
 *
 * ## 용량
 *
 * localStorage 는 오리진당 보통 5MB 다. 프롬프트는 입력 예산이 8192 토큰이라
 * 호출 하나가 20KB 를 넘기도 한다. 그래서 두 겹으로 막는다.
 *
 *   1. 저장 전에 긴 텍스트를 자른다 (CLIP). 화면에서 프롬프트 앞부분만 봐도
 *      대개 충분하고, 진행 중인 실행은 서버에서 온 원본을 그대로 쓴다.
 *   2. 그래도 넘치면 오래된 실행부터 지우고 다시 시도한다.
 *
 * 자르지 않고 저장하다 QuotaExceededError 가 나면 그 순간부터 아무것도 저장되지
 * 않는다 — 조용히 기록이 멈추는 쪽이 잘린 프롬프트보다 나쁘다.
 */

window.CREX = window.CREX || {};

CREX.store = (function () {
  const RUNS_KEY = 'crex.viz.runs.v1';
  const PREFS_KEY = 'crex.viz.prefs.v1';
  const RUN_PREFIX = 'crex.viz.run.v1.';

  const MAX_RUNS = 25;        // 목록에 남길 실행 수
  const MAX_EVENTS = 1200;    // 실행 하나당 저장할 이벤트 수
  const CLIP = 6000;          // 저장할 텍스트 필드 길이 상한 (문자)
  const CLIPPED_FIELDS = ['system', 'user', 'raw', 'code'];
  const TERMINAL_EVENTS = ['run.finished', 'run.failed', 'run.cancelling'];

  let available = true;
  try {
    window.localStorage.setItem('crex.viz.probe', '1');
    window.localStorage.removeItem('crex.viz.probe');
  } catch (err) {
    // 사생활 보호 모드나 정책으로 막힌 브라우저. 화면은 그대로 돌아가고
    // 기록만 남지 않는다.
    available = false;
  }

  function read(key, fallback) {
    if (!available) return fallback;
    try {
      const raw = window.localStorage.getItem(key);
      return raw ? JSON.parse(raw) : fallback;
    } catch (err) {
      return fallback;
    }
  }

  let pruning = false;

  function write(key, value) {
    if (!available) return false;
    const payload = JSON.stringify(value);
    try {
      window.localStorage.setItem(key, payload);
      return true;
    } catch (err) {
      // 용량 초과. 오래된 실행부터 버리고 다시 시도한다. 정리 자체도 write 를
      // 부르므로, 다시 들어오면 그냥 실패로 끝낸다 (재귀 방지).
      if (pruning) return false;
      pruning = true;
      try {
        while (pruneOldest()) {
          try {
            window.localStorage.setItem(key, payload);
            return true;
          } catch (err2) {
            /* 더 지워본다 */
          }
        }
      } finally {
        pruning = false;
      }
      return false;
    }
  }

  /*
   * 가장 오래된 실행의 **이벤트 뭉치**를 버린다. 목록(index)은 건드리지 않는다.
   *
   * 용량을 먹는 것은 프롬프트가 들어 있는 이벤트 쪽이고, 목록은 실행당 수백
   * 바이트다. 정리하면서 목록까지 고치면 정리를 유발한 그 쓰기가 바로 뒤에
   * 예전 목록으로 덮어써서 서로 어긋난다. 이벤트를 잃은 실행은 재생할 때
   * 그렇다고 알려준다.
   */
  function pruneOldest() {
    const runs = listRuns();
    for (let i = runs.length - 1; i >= 0; i -= 1) {
      const key = RUN_PREFIX + runs[i].id;
      if (window.localStorage.getItem(key) !== null) {
        window.localStorage.removeItem(key);
        return true;
      }
    }
    return false;
  }

  // ── 실행 목록 ────────────────────────────────────────────────────

  function listRuns() {
    const runs = read(RUNS_KEY, []);
    return Array.isArray(runs) ? runs : [];
  }

  function saveRun(head, extra) {
    const runs = listRuns().filter((r) => r.id !== head.id);
    runs.unshift(Object.assign({}, head, extra || {}));
    while (runs.length > MAX_RUNS) {
      const dropped = runs.pop();
      if (available) window.localStorage.removeItem(RUN_PREFIX + dropped.id);
    }
    write(RUNS_KEY, runs);
    return runs;
  }

  function dropRun(id) {
    if (available) window.localStorage.removeItem(RUN_PREFIX + id);
    write(RUNS_KEY, listRuns().filter((r) => r.id !== id));
  }

  function clearAll() {
    listRuns().forEach((r) => {
      if (available) window.localStorage.removeItem(RUN_PREFIX + r.id);
    });
    write(RUNS_KEY, []);
  }

  // ── 이벤트 ───────────────────────────────────────────────────────

  function clipEvent(event) {
    const data = event.data || {};
    let copy = null;
    CLIPPED_FIELDS.forEach((field) => {
      const value = data[field];
      if (typeof value === 'string' && value.length > CLIP) {
        copy = copy || Object.assign({}, data);
        copy[field] = value.slice(0, CLIP) + '\n\n... (기록 저장 시 잘림) ...';
      }
    });
    return copy ? Object.assign({}, event, { data: copy }) : event;
  }

  /*
   * 상한을 넘으면 앞에서부터 자르되, 실행의 결말은 반드시 남긴다.
   *
   * 앞을 남기는 이유는 청크와 초기 호출이 거기 있어서다. 그런데 판정 전체와
   * 에이전트 요약은 마지막 `run.finished` 하나에 들어 있어서, 그냥 잘랐다가는
   * 큰 실행의 기록만 골라 쓸모없어진다.
   */
  function saveEvents(runId, events) {
    let kept = events.map(clipEvent);
    if (kept.length > MAX_EVENTS) {
      const terminal = kept.filter((e) => TERMINAL_EVENTS.indexOf(e.type) >= 0);
      kept = kept.slice(0, MAX_EVENTS - terminal.length).concat(terminal);
    }
    write(RUN_PREFIX + runId, { events: kept, saved_at: Date.now() });
  }

  function loadEvents(runId) {
    const blob = read(RUN_PREFIX + runId, null);
    return blob && Array.isArray(blob.events) ? blob.events : [];
  }

  // ── 화면 설정 ────────────────────────────────────────────────────

  function prefs() {
    const value = read(PREFS_KEY, {});
    return value && typeof value === 'object' ? value : {};
  }

  function savePrefs(patch) {
    write(PREFS_KEY, Object.assign(prefs(), patch));
  }

  return {
    available: function () { return available; },
    listRuns: listRuns,
    saveRun: saveRun,
    dropRun: dropRun,
    clearAll: clearAll,
    saveEvents: saveEvents,
    loadEvents: loadEvents,
    prefs: prefs,
    savePrefs: savePrefs,
    MAX_EVENTS: MAX_EVENTS,
  };
})();
