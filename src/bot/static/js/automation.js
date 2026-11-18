// ─── 자동화 (단일 스케줄 관리) — 100% BE API 기반 렌더링 ───
(function () {
  let showScheduleForm = false;
  let scheduleNlMode = true;
  let cachedSchedules = [];
  let cachedModels = [];
  let editingScheduleId = null;

  const EFFORT_OPTIONS = [
    { value: '', label: '(기본 정책 따름)' },
    { value: 'low', label: 'Low · 빠른 응답' },
    { value: 'medium', label: 'Medium · 균형' },
    { value: 'high', label: 'High · 심층 추론' },
    { value: 'max', label: 'Max · 최대 추론' },
  ];

  const CRON_PRESETS = [
    { label: '매시 정각', cron: '0 * * * *' },
    { label: '매일 08:00', cron: '0 8 * * *' },
    { label: '평일 09:00', cron: '0 9 * * 1-5' },
    { label: '매일 22:00', cron: '0 22 * * *' },
  ];

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

  async function ensureModelsLoaded() {
    if (cachedModels.length > 0) return;
    try {
      const token = typeof getGatewayToken === 'function' ? getGatewayToken() : '';
      const headers = token ? { 'X-Auth-Token': token } : {};
      const res = await fetch('/api/status', { headers });
      if (res.ok) {
        const data = await res.json();
        if (Array.isArray(data.availableModels)) {
          cachedModels = data.availableModels;
        }
      }
    } catch (_) {}
  }

  window.loadAutomationData = async function () {
    const root = document.getElementById('automation-root');
    if (!root) return;
    root.innerHTML = `<div id="auto-body" class="auto-empty">불러오는 중…</div>`;
    try {
      const [data] = await Promise.all([apiJson('/api/schedules'), ensureModelsLoaded()]);
      cachedSchedules = data.items || [];
      renderSchedules(cachedSchedules);
    } catch (err) {
      const body = document.getElementById('auto-body');
      if (body) body.innerHTML = `<div class="auto-empty">불러오기 실패: ${esc(err.message)}</div>`;
    }
  };

  function buildModelOptionsHtml(selectedModel) {
    const options = [`<option value="">(기본 정책 따름)</option>`];
    const seen = new Set(['']);
    for (const m of cachedModels) {
      const id = String(m.id || '');
      if (!id || seen.has(id)) continue;
      seen.add(id);
      options.push(
        `<option value="${esc(id)}" ${id === selectedModel ? 'selected' : ''}>${esc(m.label || id)}</option>`
      );
    }
    if (selectedModel && !seen.has(selectedModel)) {
      options.push(`<option value="${esc(selectedModel)}" selected>${esc(selectedModel)}</option>`);
    }
    return options.join('');
  }

  function buildEffortOptionsHtml(selectedEffort) {
    const seen = new Set();
    const options = EFFORT_OPTIONS.map((opt) => {
      seen.add(opt.value);
      return `<option value="${esc(opt.value)}" ${opt.value === (selectedEffort || '') ? 'selected' : ''}>${esc(opt.label)}</option>`;
    });
    if (selectedEffort && !seen.has(selectedEffort)) {
      options.push(`<option value="${esc(selectedEffort)}" selected>${esc(selectedEffort)}</option>`);
    }
    return options.join('');
  }

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
              ${item.model ? `<span class="auto-badge">${esc(item.model)}</span>` : ''}
              ${item.effort ? `<span class="auto-badge">${esc(item.effort)}</span>` : ''}
              <span style="flex:1"></span>
              <button class="auto-btn" onclick="openEditScheduleModal('${esc(item.id)}')">수정 ✏️</button>
              <button class="auto-btn" onclick="toggleScheduleEnabled('${esc(item.id)}', ${!item.enabled})">${item.enabled ? '일시중지' : '활성화'}</button>
              <button class="auto-btn" onclick="runScheduleNow('${esc(item.id)}')">▶ 실행</button>
              <button class="auto-btn danger" onclick="deleteScheduleItem('${esc(item.id)}')">삭제</button>
            </div>
            <div class="auto-title">${esc(item.description)}</div>
            <div class="auto-sub">${esc(item.prompt)}</div>
          </div>
        `).join('')}
      ${editingScheduleId ? renderEditScheduleModal() : ''}
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

  function renderEditScheduleModal() {
    const item = cachedSchedules.find(s => s.id === editingScheduleId);
    if (!item) return '';
    return `
      <div id="auto-edit-modal" class="auto-modal-backdrop" onclick="if (event.target === this) closeEditScheduleModal()">
        <div class="auto-modal" role="dialog" aria-modal="true" aria-label="스케줄 수정">
          <div class="auto-modal-head">
            <span class="auto-modal-title">스케줄 수정 <span class="auto-mono">${esc(item.id)}</span></span>
            <button type="button" class="auto-btn" onclick="closeEditScheduleModal()">✕</button>
          </div>
          <div class="auto-field-group">
            <label class="auto-field-label" for="edit-sched-desc">설명</label>
            <input id="edit-sched-desc" class="auto-input" value="${esc(item.description)}" placeholder="스케줄 한 줄 설명">
          </div>
          <div class="auto-field-group">
            <label class="auto-field-label" for="edit-sched-cron">실행 주기 (Cron)</label>
            <input id="edit-sched-cron" class="auto-input auto-mono" value="${esc(item.cron)}" placeholder="분 시 일 월 요일 (예: 0 8 * * *)">
            <div class="auto-presets">
              ${CRON_PRESETS.map(p => `<button type="button" class="auto-preset-chip" onclick="applyEditCronPreset('${esc(p.cron)}')">${esc(p.label)} (${esc(p.cron)})</button>`).join('')}
            </div>
            <span class="auto-field-hint">5필드 cron 형식: 분(0-59) 시(0-23) 일(1-31) 월(1-12) 요일(0-6, 0=일)</span>
          </div>
          <div class="auto-field-group">
            <label class="auto-field-label" for="edit-sched-prompt">실행 프롬프트</label>
            <textarea id="edit-sched-prompt" class="auto-textarea" rows="4" placeholder="에이전트에게 시킬 작업 지시">${esc(item.prompt)}</textarea>
          </div>
          <div class="auto-grid-2">
            <div class="auto-field-group">
              <label class="auto-field-label" for="edit-sched-model">지정 모델</label>
              <select id="edit-sched-model" class="auto-select">${buildModelOptionsHtml(item.model || '')}</select>
            </div>
            <div class="auto-field-group">
              <label class="auto-field-label" for="edit-sched-effort">추론 강도 (Effort)</label>
              <select id="edit-sched-effort" class="auto-select">${buildEffortOptionsHtml(item.effort || '')}</select>
            </div>
          </div>
          <label class="auto-checkbox-row">
            <input type="checkbox" id="edit-sched-enabled" ${item.enabled ? 'checked' : ''}>
            <span>스케줄 활성화</span>
          </label>
          <div class="auto-row" style="justify-content: flex-end; margin-top: 4px;">
            <button type="button" class="auto-btn" onclick="closeEditScheduleModal()">취소</button>
            <button type="button" id="btn-save-edit-sched" class="auto-btn primary" onclick="submitEditSchedule('${esc(item.id)}')">저장</button>
          </div>
        </div>
      </div>
    `;
  }

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && editingScheduleId) {
      window.closeEditScheduleModal();
    }
  });

  window.openEditScheduleModal = async function (id) {
    await ensureModelsLoaded();
    editingScheduleId = id;
    renderSchedules(cachedSchedules);
    setTimeout(() => {
      document.getElementById('edit-sched-desc')?.focus();
    }, 0);
  };

  window.closeEditScheduleModal = function () {
    editingScheduleId = null;
    renderSchedules(cachedSchedules);
  };

  window.applyEditCronPreset = function (cronExpr) {
    const input = document.getElementById('edit-sched-cron');
    if (input) input.value = cronExpr;
  };

  window.submitEditSchedule = async function (id) {
    const description = (document.getElementById('edit-sched-desc')?.value || '').trim();
    const cron = (document.getElementById('edit-sched-cron')?.value || '').trim();
    const prompt = (document.getElementById('edit-sched-prompt')?.value || '').trim();
    const model = (document.getElementById('edit-sched-model')?.value || '').trim();
    const effort = (document.getElementById('edit-sched-effort')?.value || '').trim();
    const enabled = Boolean(document.getElementById('edit-sched-enabled')?.checked);

    if (!cron || !prompt) {
      showAutoToast('실행 주기(cron)와 작업 프롬프트를 모두 입력해 주세요.', true);
      return;
    }

    const saveBtn = document.getElementById('btn-save-edit-sched');
    if (saveBtn) saveBtn.disabled = true;
    try {
      await apiJson(`/api/schedules/${encodeURIComponent(id)}`, {
        method: 'PATCH',
        body: JSON.stringify({ description, cron, prompt, model, effort, enabled }),
      });
      editingScheduleId = null;
      showAutoToast('스케줄이 수정되었습니다.');
      await window.loadAutomationData();
    } catch (err) {
      showAutoToast(err.message, true);
      if (saveBtn) saveBtn.disabled = false;
    }
  };

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
