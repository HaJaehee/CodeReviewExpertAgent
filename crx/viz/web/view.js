/*
 * Presentation 계층 — 화면.
 *
 * 서버가 흘리는 이벤트를 상태로 접어 넣고, 바뀐 곳만 다시 그린다.
 *
 * ## 왜 이 화면인가
 *
 * MCP 도구는 에이전트에게 요약만 돌려준다. 컨텍스트를 아끼려는 의도된 설계지만,
 * 프롬프트를 다듬으려는 사람에게는 정확히 그 안쪽이 필요하다. 그래서 화면의
 * 중심은 지적 목록이 아니라 **두 모델의 호출 레인**이다.
 *
 *   생성 모델이 무엇을 보고 무엇을 뱉었는가 (왼쪽 레인)
 *   검증 모델이 그것을 왜 살렸거나 죽였는가 (오른쪽 레인)
 *
 * 호출 카드를 누르면 그 호출의 프롬프트·스키마·원문 응답이 그대로 나온다.
 * 지적이 왜 나왔는지 추측하지 않아도 되는 것이 이 화면의 존재 이유다.
 *
 * ## 호출 레인은 증분으로 그린다
 *
 * 폴더 감사는 호출이 수백 건이다. 이벤트마다 레인 전체를 다시 그리면 O(n²) 이
 * 되어 진행 중에 화면이 굳는다. 카드는 요청 때 만들고 응답 때 그 카드만 고친다.
 */

(function () {
  const store = CRX.store;
  const client = CRX.client;

  const $ = (id) => document.getElementById(id);

  function escapeHtml(value) {
    return String(value === null || value === undefined ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function clip(text, limit) {
    const value = String(text || '');
    return value.length > limit ? value.slice(0, limit - 1) + '…' : value;
  }

  function pretty(value) {
    try {
      return JSON.stringify(value, null, 2);
    } catch (err) {
      return String(value);
    }
  }

  const STAGE_LABEL = { chunk: '청킹', ground: '그라운딩', generate: '생성', filter: '검증' };
  const SEVERITY_LABEL = { high: '높음', medium: '중간', low: '낮음' };
  const REJECT_LABEL = {
    line_out_of_range: '라인 범위 밖 (환각)',
    line_not_changed: '변경되지 않은 라인',
    verdict_no: '검증자 기각',
    code_not_found: '코드가 스니펫에 없음',
    duplicate: '중복 지적',
    filter_error: '검증 호출 실패 → 보수적 기각',
  };

  // ── 상태 ──────────────────────────────────────────────────────────

  let config = null;
  let stopFollow = null;
  let verdictFilter = 'all';
  let state = blankState();

  function blankState() {
    return {
      runId: null,
      label: '',
      status: 'idle',
      live: false,
      groundingEnabled: true,
      chunkCount: 0,
      staticCount: 0,
      generated: 0,
      calls: {},          // call_id → { role, request, response, error }
      chunks: [],
      verdicts: [],
      result: null,
      agentSummary: '',
      counts: { generator: 0, verifier: 0 },
      liveRejects: 0,
    };
  }

  // ── 이벤트 적용 ───────────────────────────────────────────────────

  const HANDLERS = {
    'run.started': (data) => {
      state.label = data.label || '';
      state.groundingEnabled = !(data.config && data.config.grounding_enabled === false);
      $('run-label').textContent = data.label || '';
      setStatus('running');
      if (!state.groundingEnabled) setStage('ground', 'skipped', '꺼짐');
    },

    'chunks.planned': (data) => {
      state.chunkCount = data.count;
      $('m-chunks').textContent = data.count;
      $('chunk-meta').textContent = data.count + '개 · 파일 ' + (data.files || []).length + '개';
    },

    'stage.started': (data) => {
      if (data.stage === 'ground' && !state.groundingEnabled) return;
      setStage(data.stage, 'running', '');
    },

    'stage.finished': (data) => {
      if (data.stage === 'ground' && !state.groundingEnabled) return;
      setStage(data.stage, 'done', data.seconds.toFixed(2) + 's');
    },

    'chunk.ready': (data) => {
      state.chunks.push(data);
      state.staticCount += (data.static_findings || []).length;
      $('m-static').textContent = state.staticCount;
      $('chunks').appendChild(chunkNode(data));
    },

    'llm.request': (data) => {
      state.calls[data.call_id] = { role: data.role, request: data, response: null, error: null };
      state.counts[data.role] += 1;
      laneMeta(data.role);
      appendCall(data);
    },

    'llm.response': (data) => {
      const call = state.calls[data.call_id];
      if (call) call.response = data;
      if (data.role === 'generator') {
        const found = ((data.parsed || {}).findings || []).length;
        state.generated += found;
        $('m-generated').textContent = state.generated;
      } else if (isRejection(data.parsed)) {
        state.liveRejects += 1;
        $('m-llm-reject').textContent = state.liveRejects;
      }
      updateCall(data.call_id);
    },

    'llm.error': (data) => {
      const call = state.calls[data.call_id];
      if (call) call.error = data;
      updateCall(data.call_id);
    },

    // 중단 요청 뒤에도 진행 중인 호출이 끝날 때까지는 실행 중이다. 이때
    // '리뷰 시작' 이 풀리면 실행이 겹치므로 버튼은 잠근 채로 둔다.
    'run.cancelling': () => setStatus('cancelled', '중단하는 중', true),

    'run.failed': (data) => {
      setStatus('failed');
      clearRunningStages();
      toast(data.error, 'error');
      $('agent-summary').textContent = data.error;
    },

    'run.finished': (data) => {
      setStatus(data.status);
      clearRunningStages();
      state.verdicts = data.verdicts || [];
      state.result = data.result;
      state.agentSummary = data.agent_summary || '';
      $('agent-summary').textContent = state.agentSummary || '—';
      renderResult();
    },
  };

  function apply(event) {
    const handler = HANDLERS[event.type];
    if (handler) handler(event.data || {}, event);
  }

  function isRejection(parsed) {
    if (!parsed) return false;
    return parsed.code_present === false || parsed.verdict !== 'yes';
  }

  // ── 파이프라인 레일 ───────────────────────────────────────────────

  function setStage(stage, stageState, note) {
    const node = document.querySelector('.stage[data-stage="' + stage + '"]');
    if (!node) return;
    node.dataset.state = stageState;
    node.querySelector('.stage-time').textContent = note || '';
  }

  function clearRunningStages() {
    document.querySelectorAll('.stage[data-state="running"]').forEach((node) => {
      node.dataset.state = 'done';
    });
  }

  function resetRail() {
    document.querySelectorAll('.stage').forEach((node) => {
      node.removeAttribute('data-state');
      node.querySelector('.stage-time').textContent = '';
    });
  }

  function setStatus(status, text, busy) {
    state.status = status;
    const label = {
      idle: '대기', running: '진행 중', done: '완료', failed: '실패', cancelled: '중단됨',
    }[status] || status;
    const node = $('run-status');
    node.dataset.state = status;
    node.textContent = text || label;

    const running = busy !== undefined ? busy : status === 'running';
    $('btn-cancel').disabled = status !== 'running';
    $('btn-run').disabled = running;
  }

  // ── 호출 레인 ─────────────────────────────────────────────────────

  function laneMeta(role) {
    $(role === 'generator' ? 'gen-meta' : 'ver-meta').textContent = '호출 ' + state.counts[role];
  }

  function callWhere(data) {
    if (data.target && data.target.line) {
      return (data.path || '') + ':' + data.target.line;
    }
    return data.path || data.chunk_id || '(청크 미상)';
  }

  function appendCall(data) {
    const list = $('calls-' + data.role);
    const placeholder = list.querySelector('.empty-lane');
    if (placeholder) placeholder.remove();

    const item = document.createElement('li');
    item.className = 'call';
    item.dataset.state = 'pending';
    item.dataset.callId = data.call_id;
    item.innerHTML =
      '<div class="call-top">' +
        '<span class="call-where">' + escapeHtml(callWhere(data)) + '</span>' +
        '<span class="call-time">…</span>' +
      '</div>' +
      '<div class="call-body">' + escapeHtml(callSubtitle(data)) + '</div>' +
      '<div class="call-tags"></div>';
    item.addEventListener('click', () => openDrawer(data.call_id));
    list.appendChild(item);
    list.scrollTop = list.scrollHeight;
  }

  function callSubtitle(data) {
    if (data.role === 'verifier' && data.target) {
      return '[' + (data.target.rule_id || '?') + '] ' + clip(data.target.message, 90);
    }
    return data.chunk_id ? '청크 ' + data.chunk_id : '요청 전송';
  }

  function updateCall(callId) {
    const call = state.calls[callId];
    const node = document.querySelector('.call[data-call-id="' + callId + '"]');
    if (!call || !node) return;

    const tags = node.querySelector('.call-tags');
    const time = node.querySelector('.call-time');

    if (call.error) {
      node.dataset.state = 'error';
      time.textContent = call.error.latency_ms + 'ms';
      node.querySelector('.call-body').textContent = clip(call.error.error, 120);
      tags.innerHTML = '<span class="tag tag-no">실패</span>';
      return;
    }
    if (!call.response) return;

    const parsed = call.response.parsed || {};
    time.textContent = call.response.latency_ms + 'ms';

    if (call.role === 'generator') {
      const findings = parsed.findings || [];
      node.dataset.state = 'ok';
      node.querySelector('.call-body').textContent = findings.length
        ? findings.map((f) => '[' + f.rule_id + '] ' + clip(f.message, 60)).join(' · ')
        : '지적 없음 — 빈 목록이 정상이며 바람직하다';
      tags.innerHTML = '<span class="tag ' + (findings.length ? 'tag-count' : 'tag-zero') + '">지적 '
        + findings.length + '건</span>';
      return;
    }

    const rejected = isRejection(parsed);
    node.dataset.state = rejected ? 'no' : 'yes';
    node.querySelector('.call-body').textContent = clip(parsed.reason || '', 140);
    tags.innerHTML =
      '<span class="tag ' + (rejected ? 'tag-no' : 'tag-yes') + '">' +
        (parsed.verdict === 'yes' ? 'verdict: yes' : 'verdict: no') + '</span>' +
      '<span class="tag ' + (parsed.code_present === false ? 'tag-no' : '') + '">code_present: ' +
        (parsed.code_present === false ? 'false' : 'true') + '</span>';
  }

  // ── 청크 ──────────────────────────────────────────────────────────

  function chunkNode(data) {
    const node = document.createElement('details');
    node.className = 'chunk';

    const statics = data.static_findings || [];
    const tags =
      '<span class="tag">' + (data.language || '') + '</span>' +
      '<span class="tag">변경 ' + (data.changed_lines || []).length + '줄</span>' +
      (statics.length ? '<span class="tag tag-count">정적분석 ' + statics.length + '</span>' : '');

    node.innerHTML =
      '<summary>' +
        '<span>' + escapeHtml(data.path) + ':' + data.start_line + '-' + data.end_line + '</span>' +
        '<span class="c-sym">' + escapeHtml(data.enclosing_symbol || '') + '</span>' +
        '<span class="c-tags">' + tags + '</span>' +
      '</summary>' +
      (statics.length
        ? '<ul class="static-list">' + statics.map((f) =>
            '<li>[' + escapeHtml(f.tool) + ':' + escapeHtml(f.rule_id) + '] @' + f.line +
            ' — ' + escapeHtml(f.message) + '</li>').join('') + '</ul>'
        : '') +
      '<pre>' + renderCode(data.code || '') + '</pre>';
    return node;
  }

  // 라인 주석 `[added @142] ...` 을 상태별로 물들인다. 이 형식이 프롬프트
  // 계약 그 자체라(`wiki/invariants.md`) 화면에서도 그대로 보여준다.
  function renderCode(code) {
    return code.split('\n').map((line) => {
      let cls = '';
      if (line.startsWith('[added')) cls = 'ln-added';
      else if (line.startsWith('[deleted')) cls = 'ln-deleted';
      return cls ? '<span class="' + cls + '">' + escapeHtml(line) + '</span>' : escapeHtml(line);
    }).join('\n');
  }

  // ── 판정 ──────────────────────────────────────────────────────────

  function renderResult() {
    const result = state.result || {};
    const kept = (result.kept || []).length;
    const deterministic = state.verdicts.filter((v) => !v.kept && v.short_circuited).length;
    const llmRejects = state.verdicts.filter((v) => !v.kept && !v.short_circuited).length;

    $('m-kept').textContent = kept;
    $('m-deterministic').textContent = deterministic;
    $('m-llm-reject').textContent = llmRejects;
    $('m-reject-rate').textContent = result.total_raw
      ? Math.round((result.reject_rate || 0) * 100) + '%'
      : '—';
    if (result.static_findings_count !== undefined) {
      $('m-static').textContent = result.static_findings_count;
    }
    renderVerdicts();
  }

  function renderVerdicts() {
    const body = $('verdicts');
    const rows = state.verdicts.filter((v) =>
      verdictFilter === 'all' || (verdictFilter === 'kept') === v.kept);

    if (!rows.length) {
      body.innerHTML = '<tr class="empty"><td colspan="6">' +
        (state.verdicts.length ? '이 조건에 맞는 항목이 없다.' : '아직 없다.') + '</td></tr>';
      return;
    }

    body.innerHTML = rows.map((v) =>
      '<tr data-kept="' + v.kept + '">' +
        '<td class="c-where">' + escapeHtml(v.path) + ':' + v.line + '</td>' +
        '<td><span class="sev" data-sev="' + v.severity + '">' +
          (SEVERITY_LABEL[v.severity] || v.severity) + '</span></td>' +
        '<td class="c-rule">' + escapeHtml(v.rule_id) + '</td>' +
        '<td class="c-msg">' + escapeHtml(v.message) + '</td>' +
        '<td>' + (v.kept
          ? '<span class="tag tag-yes">유지</span>'
          : '<span class="tag tag-no">' +
            escapeHtml(REJECT_LABEL[v.reject_reason] || '기각') + '</span>') + '</td>' +
        '<td class="c-reason">' + escapeHtml(v.reason || '') + '</td>' +
      '</tr>').join('');
  }

  // ── 상세 서랍 ─────────────────────────────────────────────────────

  let drawerCall = null;
  let drawerPane = 'user';

  function openDrawer(callId) {
    const call = state.calls[callId];
    if (!call) return;
    drawerCall = call;
    const request = call.request;
    $('drawer-title').textContent =
      (call.role === 'generator' ? '생성 모델 호출' : '검증 모델 호출') + ' #' + callId;
    $('drawer-sub').textContent =
      request.model + ' · ' + request.base_url + ' · schema=' + request.schema_name +
      (request.chunk_id ? ' · ' + request.chunk_id : '');
    $('drawer').hidden = false;
    paintDrawer();
  }

  function paintDrawer() {
    if (!drawerCall) return;
    document.querySelectorAll('#drawer-tabs .tab').forEach((tab) => {
      tab.classList.toggle('is-on', tab.dataset.pane === drawerPane);
    });

    const request = drawerCall.request;
    const response = drawerCall.response;
    let text = '';
    if (drawerPane === 'user') text = request.user;
    else if (drawerPane === 'system') text = request.system;
    else if (drawerPane === 'schema') text = pretty(request.schema);
    else if (drawerPane === 'parsed') {
      text = response ? pretty(response.parsed)
        : (drawerCall.error ? drawerCall.error.error : '아직 응답이 없다.');
    } else if (drawerPane === 'raw') {
      text = response ? (response.raw || '(원문이 기록되지 않았다)')
        : (drawerCall.error ? drawerCall.error.error : '아직 응답이 없다.');
    }
    $('drawer-pre').textContent = text || '';
  }

  function closeDrawer() {
    $('drawer').hidden = true;
    drawerCall = null;
  }

  // ── 실행 ──────────────────────────────────────────────────────────

  function collectParams() {
    const kind = $('kind').value;
    const params = {};
    const paths = $('paths').value.split(',').map((s) => s.trim()).filter(Boolean);
    if (paths.length) params.paths = paths;

    if (kind === 'diff') {
      params.from_ref = $('from-ref').value.trim();
      params.to_ref = $('to-ref').value.trim() || 'HEAD';
      params.use_merge_base = $('merge-base').checked;
      if (!params.from_ref) throw new Error('from 참조를 입력하라 (예: main)');
    } else if (kind === 'file' || kind === 'directory') {
      params.path = $('path').value.trim();
      if (!params.path) throw new Error('경로를 입력하라');
      if (kind === 'directory') params.recursive = $('recursive').checked;
      delete params.paths;
    }
    return { kind: kind, params: params };
  }

  async function startRun() {
    let request;
    try {
      request = collectParams();
    } catch (err) {
      toast(err.message, 'error');
      return;
    }

    resetView();
    savePrefs();

    let head;
    try {
      head = await client.start(request.kind, request.params);
    } catch (err) {
      toast(err.message, 'error');
      setStatus('failed');
      return;
    }

    state.runId = head.id;
    state.live = true;
    setStatus('running');
    store.saveRun(head, { local_started_at: Date.now() });
    renderHistory();
    followRun(head.id, true);
  }

  function followRun(runId, persist) {
    if (stopFollow) stopFollow();
    const collected = [];
    let warnedDropped = false;

    stopFollow = client.follow(
      runId,
      (events, payload) => {
        events.forEach((event) => {
          collected.push(event);
          apply(event);
        });
        // 상한에 걸려 버려진 이벤트가 있으면 알린다. 화면이 조용히 불완전해지는
        // 것이 가장 나쁘다 — 호출 몇 건이 안 보이는 걸 버그로 오해하게 된다.
        if (payload.run.dropped_events && !warnedDropped) {
          warnedDropped = true;
          toast('이벤트가 너무 많아 ' + payload.run.dropped_events +
            '건이 화면에서 생략됐다. 전체 리포트는 파일로 나온다.', 'warn');
        }
      },
      (payload) => {
        stopFollow = null;
        if (persist) {
          store.saveEvents(runId, collected);
          store.saveRun(payload.run, { metrics: snapshotMetrics() });
          renderHistory();
        }
      },
      (err) => {
        stopFollow = null;
        toast('이벤트 수신 실패: ' + err.message, 'error');
        setStatus('failed');
      }
    );
  }

  function snapshotMetrics() {
    const result = state.result || {};
    return {
      chunks: state.chunkCount,
      generated: state.generated,
      kept: (result.kept || []).length,
      rejected: (result.rejected || []).length,
      reject_rate: result.reject_rate,
    };
  }

  async function cancelRun() {
    if (!state.runId) return;
    try {
      await client.cancel(state.runId);
      toast('중단을 요청했다. 진행 중인 호출이 끝나면 멈춘다.', 'ok');
    } catch (err) {
      toast(err.message, 'error');
    }
  }

  // ── 기록 ──────────────────────────────────────────────────────────

  function renderHistory() {
    const runs = store.listRuns();
    $('history-count').textContent = runs.length;
    const list = $('history');

    if (!runs.length) {
      list.innerHTML = '<li class="empty-lane" style="cursor:default;border:none;background:none">' +
        (store.available() ? '아직 없다.' : 'localStorage 를 쓸 수 없어 기록이 남지 않는다.') + '</li>';
      return;
    }

    list.innerHTML = runs.map((run) => {
      const metrics = run.metrics || {};
      const when = new Date((run.local_started_at || run.created_at * 1000)).toLocaleString('ko-KR', {
        month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
      });
      const sub = metrics.kept === undefined
        ? (run.status === 'running' ? '진행 중' : (run.error ? '실패' : '기록 없음'))
        : '청크 ' + metrics.chunks + ' · 생성 ' + metrics.generated + ' · 유지 ' + metrics.kept;
      return '<li data-run="' + run.id + '" class="' + (run.id === state.runId ? 'is-on' : '') + '">' +
        '<div class="h-top">' +
          '<span class="h-label">' + escapeHtml(run.label || run.kind) + '</span>' +
          '<span class="h-when">' + escapeHtml(when) + '</span>' +
        '</div>' +
        '<div class="h-sub">' + escapeHtml(sub) + '</div>' +
      '</li>';
    }).join('');

    list.querySelectorAll('li[data-run]').forEach((node) => {
      node.addEventListener('click', () => replay(node.dataset.run));
    });
  }

  /*
   * 기록에서 고른 실행을 다시 재생한다.
   *
   * 이벤트를 그대로 다시 흘려보내면 화면이 저절로 그때 모습으로 재구성된다 —
   * 재생 전용 코드를 따로 두지 않아도 되고, 따라서 재생 화면이 실시간 화면과
   * 어긋날 일이 없다. 아직 진행 중인 실행이면 서버를 이어서 따라간다.
   */
  function replay(runId) {
    const events = store.loadEvents(runId);
    const head = store.listRuns().find((r) => r.id === runId);

    resetView();
    state.runId = runId;
    renderHistory();

    if (!events.length && head && head.status === 'running') {
      followRun(runId, true);
      return;
    }
    if (!events.length) {
      toast('저장된 이벤트가 없다. 서버 기록도 이미 사라졌을 수 있다.', 'error');
      setStatus(head ? head.status : 'failed');
      return;
    }
    events.forEach(apply);
    if (state.status === 'running') setStatus(head ? head.status : 'done');
  }

  function resetView() {
    if (stopFollow) { stopFollow(); stopFollow = null; }
    state = blankState();
    resetRail();
    ['m-chunks', 'm-static', 'm-generated', 'm-deterministic', 'm-llm-reject', 'm-kept']
      .forEach((id) => { $(id).textContent = '0'; });
    $('m-reject-rate').textContent = '—';
    $('calls-generator').innerHTML = '<li class="empty-lane">아직 호출이 없다.</li>';
    $('calls-verifier').innerHTML = '<li class="empty-lane">아직 호출이 없다.</li>';
    $('chunks').innerHTML = '';
    $('chunk-meta').textContent = '0개';
    $('gen-meta').textContent = '호출 0';
    $('ver-meta').textContent = '호출 0';
    $('agent-summary').textContent = '—';
    $('run-label').textContent = '';
    $('verdicts').innerHTML = '<tr class="empty"><td colspan="6">아직 없다.</td></tr>';
    closeDrawer();
    setStatus('idle');
  }

  // ── 폼 ────────────────────────────────────────────────────────────

  function syncForm() {
    const kind = $('kind').value;
    document.querySelector('[data-for="diff"]').hidden = kind !== 'diff';
    document.querySelector('[data-for="path"]').hidden = !(kind === 'file' || kind === 'directory');
    document.querySelector('[data-for="directory"]').hidden = kind !== 'directory';
    document.querySelector('[data-for="paths"]').hidden = kind === 'file' || kind === 'directory';
  }

  function savePrefs() {
    store.savePrefs({
      kind: $('kind').value,
      from_ref: $('from-ref').value,
      to_ref: $('to-ref').value,
      path: $('path').value,
      paths: $('paths').value,
      merge_base: $('merge-base').checked,
      recursive: $('recursive').checked,
    });
  }

  function loadPrefs() {
    const prefs = store.prefs();
    if (prefs.kind) $('kind').value = prefs.kind;
    $('from-ref').value = prefs.from_ref || '';
    $('to-ref').value = prefs.to_ref || '';
    $('path').value = prefs.path || '';
    $('paths').value = prefs.paths || '';
    if (prefs.merge_base !== undefined) $('merge-base').checked = prefs.merge_base;
    if (prefs.recursive !== undefined) $('recursive').checked = prefs.recursive;
    syncForm();
  }

  // ── 토스트 ────────────────────────────────────────────────────────

  let toastTimer = null;
  function toast(message, kind) {
    const node = $('toast');
    node.textContent = message;
    node.dataset.kind = kind || 'warn';
    node.hidden = false;
    if (toastTimer) window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => { node.hidden = true; }, 6000);
  }

  // ── 기동 ──────────────────────────────────────────────────────────

  async function boot() {
    resetView();
    loadPrefs();
    renderHistory();

    try {
      config = await client.config();
    } catch (err) {
      toast('설정을 읽지 못했다: ' + err.message, 'error');
      return;
    }

    $('chip-repo').textContent = config.repo_root;
    $('chip-repo').title = config.repo_root;
    $('chip-generator').textContent = '생성 ' + config.config.generator.model;
    $('chip-generator').title = config.config.generator.base_url;
    $('chip-verifier').textContent = '검증 ' + config.config.verifier.model;
    $('chip-verifier').title = config.config.verifier.base_url;
    $('run-hint').innerHTML =
      '룰 ' + config.taxonomy.rules + '개 · 최소 심각도 <code>' +
      escapeHtml(config.config.min_severity) + '</code> · 동시 실행 ' +
      config.config.max_workers + ' · 설정 <code>' +
      escapeHtml(config.config.source || '기본값') + '</code>';

    // 서버가 아직 들고 있는 실행이 있으면 이어서 본다. 새로고침으로 화면이
    // 죽어도 진행 중인 리뷰는 서버에서 계속 돌고 있다.
    try {
      const running = (await client.runs()).runs.filter((r) => r.status === 'running');
      if (running.length) {
        state.runId = running[0].id;
        $('run-label').textContent = running[0].label;
        setStatus('running');
        followRun(running[0].id, true);
        toast('진행 중인 실행에 다시 붙었다.', 'ok');
      }
    } catch (err) {
      /* 목록을 못 읽어도 화면은 쓸 수 있다 */
    }
  }

  // ── 배선 ──────────────────────────────────────────────────────────

  $('kind').addEventListener('change', () => { syncForm(); savePrefs(); });
  $('btn-run').addEventListener('click', startRun);
  $('btn-cancel').addEventListener('click', cancelRun);
  $('btn-clear').addEventListener('click', () => {
    store.clearAll();
    renderHistory();
    toast('기록을 비웠다.', 'ok');
  });

  $('btn-health').addEventListener('click', async () => {
    const button = $('btn-health');
    button.disabled = true;
    button.textContent = '점검 중…';
    try {
      const result = await client.health();
      ['generator', 'verifier'].forEach((role) => {
        const chip = $('chip-' + role);
        chip.dataset.ok = result.checks[role].ok;
        chip.title = result.checks[role].base_url + ' — ' + result.checks[role].detail;
      });
      toast(result.ok ? '두 엔드포인트 모두 응답했다.'
        : '응답하지 않는 엔드포인트가 있다. 칩에 마우스를 올려 사유를 보라.',
        result.ok ? 'ok' : 'error');
    } catch (err) {
      toast(err.message, 'error');
    } finally {
      button.disabled = false;
      button.textContent = '엔드포인트 점검';
    }
  });

  $('verdict-tabs').addEventListener('click', (event) => {
    const tab = event.target.closest('.tab');
    if (!tab) return;
    verdictFilter = tab.dataset.filter;
    document.querySelectorAll('#verdict-tabs .tab').forEach((node) => {
      node.classList.toggle('is-on', node === tab);
    });
    renderVerdicts();
  });

  $('drawer-tabs').addEventListener('click', (event) => {
    const tab = event.target.closest('.tab');
    if (!tab) return;
    drawerPane = tab.dataset.pane;
    paintDrawer();
  });

  $('drawer-close').addEventListener('click', closeDrawer);
  $('drawer-scrim').addEventListener('click', closeDrawer);
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') closeDrawer();
  });

  boot();
})();
