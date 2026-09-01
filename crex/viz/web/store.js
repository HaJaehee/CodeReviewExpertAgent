/*
 * Presentation 계층의 저장소 — 서버 측 로컬 SQLite DB 연동.
 *
 * 브라우저 localStorage 의 5MB 용량 한계와 6,000자 프롬프트 강제 절단을 완전히 없애고,
 * 서버 로컬 SQLite DB (/api/history, /api/prefs) 와 연동하여 원문 프롬프트와 이벤트를
 * 손실 없이 영속적으로 관리한다.
 */

window.CREX = window.CREX || {};

CREX.store = (function () {
  'use strict';

  let memoryRuns = [];
  let memoryPrefs = {};
  let isLoaded = false;

  async function syncFromDB() {
    try {
      const [hist, pr] = await Promise.all([
        CREX.client.history(50),
        CREX.client.getPrefs(),
      ]);
      memoryRuns = (hist && Array.isArray(hist.runs)) ? hist.runs : [];
      memoryPrefs = (pr && typeof pr === 'object') ? pr : {};
      isLoaded = true;
    } catch (err) {
      // 서버 통신 오류 시 메모리 상태 유지
    }
  }

  function listRuns() {
    return memoryRuns;
  }

  async function listRunsAsync() {
    await syncFromDB();
    return memoryRuns;
  }

  async function saveRun(head, extra) {
    const runObj = Object.assign({}, head, extra || {});
    memoryRuns = memoryRuns.filter((r) => r.id !== runObj.id);
    memoryRuns.unshift(runObj);
    return memoryRuns;
  }

  async function dropRun(id) {
    memoryRuns = memoryRuns.filter((r) => r.id !== id);
    try {
      await CREX.client.deleteHistory(id);
    } catch (err) {
      console.warn('DB 실행 삭제 실패:', err);
    }
    return memoryRuns;
  }

  async function clearAll() {
    memoryRuns = [];
    try {
      await CREX.client.clearHistory();
    } catch (err) {
      console.warn('DB 전체 삭제 실패:', err);
    }
  }

  function saveEvents(runId, events) {
    // 서버에서 원본 전체 이벤트를 DB에 직접 저장하므로 브라우저 절단 저장을 생략한다.
  }

  async function loadEvents(runId) {
    try {
      const res = await CREX.client.historyEvents(runId);
      return res && Array.isArray(res.events) ? res.events : [];
    } catch (err) {
      console.warn('DB 이벤트 로드 실패:', err);
      return [];
    }
  }

  function prefs() {
    return memoryPrefs;
  }

  async function savePrefs(patch) {
    Object.assign(memoryPrefs, patch);
    try {
      await CREX.client.savePrefs(patch);
    } catch (err) {
      console.warn('DB 설정 저장 실패:', err);
    }
  }

  return {
    available: function () { return true; },
    syncFromDB: syncFromDB,
    listRuns: listRuns,
    listRunsAsync: listRunsAsync,
    saveRun: saveRun,
    dropRun: dropRun,
    clearAll: clearAll,
    saveEvents: saveEvents,
    loadEvents: loadEvents,
    prefs: prefs,
    savePrefs: savePrefs,
  };
})();
