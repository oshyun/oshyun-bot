// ─── 자동화 (단일 스케줄 관리) — 100% BE API 기반 렌더링 ───
(function () {
  let showScheduleForm = false;
  let scheduleNlMode = true;

  function esc(str) {
    return String(str ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  async function apiJson(url, options = {}) {
    const token = typeof getGatewayToken === 'function' ? getGatewayToken() : '';
    const headers = Object.assign(
      { 'Content-Type': 'application/json' },
      token ? { 'X-Auth-Token': token } : {},
      options.headers || {}
    );
    const res = await fetch(url, Object.assign({}, options, { headers }));
    const data = await res.json();
    if (!res.ok || data.status !== 'ok') {
      throw new Error(data.error || `HTTP ${res.status}`);
    }
    return data;
  }

  function showAutoToast(msg, isError = false) {
    if (typeof showToast === 'function') {
      showToast(msg, isError ? 'error' : 'info');
    } else {
      alert(msg);
    }
  }

  window.loadAutomationData = async function () {
    const root = document.getElementById('automation-root');
    if (!root) return;
    root.innerHTML = `<div id="auto-body" class="auto-empty">불러오는 중…</div>`;
    try {
      const data = await apiJson('/api/schedules');
      renderSchedules(data.items || []);
    } catch (err) {
      const body = document.getElementById('auto-body');
      if (body) body.innerHTML = `<div class="auto-empty">불러오기 실패: ${esc(err.message)}</div>`;
    }
  };

  function renderSchedules(items) {
    const body = document.getElementById('auto-body');
    if (!body) return;
    body.className = '';
    body.innerHTML = `
      <div class="auto-header">
        <span class="auto-title">등록된 스케줄 (${items.length}건)</span>
        <span style="flex:1"></span>
        <button class="auto-btn" onclick="loadAutomationData()">새로고침</button>
        <button class="auto-btn primary" onclick="toggleScheduleForm()">${showScheduleForm ? '닫기' : '+ 새 스케줄'}</button>
      </div>
      ${showScheduleForm ? renderScheduleForm() : ''}
      ${items.length === 0
        ? '<div class="auto-empty">등록된 스케줄이 없습니다.</div>'
        : items.map(item => `
          <div class="auto-card" style="margin-bottom: 10px;">
            <div class="auto-row">
              <span class="auto-badge ${item.enabled ? 'on' : ''}">${esc(item.statusLabel)}</span>
              <span class="auto-mono">${esc(item.cron)}</span>
              <span style="flex:1"></span>
              <button class="auto-btn" onclick="toggleScheduleEnabled('${esc(item.id)}', ${!item.enabled})">${item.enabled ? '일시중지' : '활성화'}</button>
              <button class="auto-btn" onclick="runScheduleNow('${esc(item.id)}')">▶ 실행</button>
              <button class="auto-btn danger" onclick="deleteScheduleItem('${esc(item.id)}')">삭제</button>
            </div>
            <div class="auto-title">${esc(item.description)}</div>
            <div class="auto-sub">${esc(item.prompt)}</div>
          </div>
        `).join('')}
    `;
  }

  function renderScheduleForm() {
    return `
      <div class="auto-form" style="margin-bottom: 14px;">
        <div class="auto-row">
          <button class="auto-tab-btn ${scheduleNlMode ? 'active' : ''}" onclick="setScheduleNlMode(true)">자연어로 등록</button>
          <button class="auto-tab-btn ${!scheduleNlMode ? 'active' : ''}" onclick="setScheduleNlMode(false)">직접 Cron 입력</button>
        </div>
        ${scheduleNlMode ? `
          <textarea id="auto-nl-input" class="auto-textarea" rows="3" placeholder="예: 매일 아침 8시에 오늘 캘린더 일정과 할 일 요약해줘"></textarea>
        ` : `
          <input id="auto-cron-input" class="auto-input" placeholder="Cron (분 시 일 월 요일, 예: 0 8 * * *)">
          <input id="auto-desc-input" class="auto-input" placeholder="설명 (선택)">
          <textarea id="auto-prompt-input" class="auto-textarea" rows="3" placeholder="에이전트 실행 프롬프트"></textarea>
        `}
        <div class="auto-row" style="justify-content: flex-end;">
          <button class="auto-btn primary" onclick="submitNewSchedule()">등록</button>
        </div>
      </div>
    `;
  }

  window.toggleScheduleForm = function () { showScheduleForm = !showScheduleForm; window.loadAutomationData(); };
  window.setScheduleNlMode = function (val) { scheduleNlMode = val; window.loadAutomationData(); };

  window.submitNewSchedule = async function () {
    try {
      const payload = scheduleNlMode
        ? { naturalText: (document.getElementById('auto-nl-input')?.value || '').trim() }
        : {
            cron: (document.getElementById('auto-cron-input')?.value || '').trim(),
            description: (document.getElementById('auto-desc-input')?.value || '').trim(),
            prompt: (document.getElementById('auto-prompt-input')?.value || '').trim(),
          };
      await apiJson('/api/schedules', { method: 'POST', body: JSON.stringify(payload) });
      showScheduleForm = false;
      showAutoToast('스케줄이 등록되었습니다.');
      window.loadAutomationData();
    } catch (err) {
      showAutoToast(err.message, true);
    }
  };

  window.toggleScheduleEnabled = async function (id, enabled) {
    try {
      await apiJson(`/api/schedules/${encodeURIComponent(id)}`, { method: 'PATCH', body: JSON.stringify({ enabled }) });
      window.loadAutomationData();
    } catch (err) { showAutoToast(err.message, true); }
  };

  window.runScheduleNow = async function (id) {
    try {
      const res = await apiJson(`/api/schedules/${encodeURIComponent(id)}/run`, { method: 'POST' });
      showAutoToast(res.message || '실행을 시작했습니다.');
    } catch (err) { showAutoToast(err.message, true); }
  };

  window.deleteScheduleItem = async function (id) {
    try {
      await apiJson(`/api/schedules/${encodeURIComponent(id)}`, { method: 'DELETE' });
      showAutoToast('스케줄을 삭제했습니다.');
      window.loadAutomationData();
    } catch (err) { showAutoToast(err.message, true); }
  };
})();
