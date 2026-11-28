/**
 * 시스템 프롬프트 & 인스트럭션 센터 (Prompts Center) 프론트엔드 컨트롤러
 * 시스템 프롬프트 및 인스트럭션을 투명하게 확인하고 수정/복원하는 통합 인터페이스
 */

(function () {
  let activePromptId = 'common';
  let activePromptTab = 'edit';
  let cachedPromptsList = [];
  let currentPromptDetail = null;

  async function openPromptsCenterModal(targetId = null) {
    const modal = document.getElementById('modal-prompts-center');
    if (!modal) return;
    modal.classList.add('open');

    if (targetId) {
      activePromptId = String(targetId).trim();
    } else if (!activePromptId) {
      activePromptId = 'common';
    }

    switchPromptTab('edit');
    await loadPromptsList();
  }

  function closePromptsCenterModal() {
    const modal = document.getElementById('modal-prompts-center');
    if (modal) modal.classList.remove('open');
  }

  async function loadPromptsList() {
    try {
      const res = await fetch('/api/prompts');
      const data = await res.json();
      if (data && data.ok && Array.isArray(data.prompts)) {
        cachedPromptsList = data.prompts;
        renderPromptsSidebar();
        // 현재 선택된 항목 로드
        await selectPromptLayer(activePromptId, false);
      }
    } catch (err) {
      console.warn('[PromptsCenter] 목록 로드 실패:', err);
    }
  }

  function renderPromptsSidebar() {
    const container = document.getElementById('prompts-center-sidebar-content');
    if (!container) return;

    const globals = cachedPromptsList.filter(p => p.category === 'global');
    const channels = cachedPromptsList.filter(p => p.category === 'channel');
    const agents = cachedPromptsList.filter(p => p.category === 'agent');

    const renderItem = (item) => {
      const isSel = item.id === activePromptId;
      const safeId = escapeHtml(item.id);
      const name = escapeHtml(item.name);
      const emoji = item.emoji || '📄';
      const isCustom = Boolean(item.isCustomized);
      const chars = item.charCount || 0;

      return `
        <button type="button" class="prompt-item-btn ${isSel ? 'active' : ''}" onclick="window.selectPromptLayer('${safeId}')">
          <div class="prompt-item-icon">${escapeHtml(emoji)}</div>
          <div class="prompt-item-info">
            <div class="prompt-item-name-row">
              <span class="prompt-item-name">${name}</span>
              ${isCustom ? '<span class="prompt-item-badge custom">수정됨</span>' : ''}
            </div>
            <div class="prompt-item-desc">${chars.toLocaleString()}자 · ${escapeHtml(item.injectScope || '')}</div>
          </div>
        </button>
      `;
    };

    let html = '';

    if (globals.length > 0) {
      html += `
        <div class="prompts-sidebar-section">
          <div class="prompts-sidebar-sec-title">공통 상속 시스템 지침</div>
          <div class="prompts-list-group">${globals.map(renderItem).join('')}</div>
        </div>
      `;
    }

    if (channels.length > 0) {
      html += `
        <div class="prompts-sidebar-section">
          <div class="prompts-sidebar-sec-title">채널 및 모드 기본 지침</div>
          <div class="prompts-list-group">${channels.map(renderItem).join('')}</div>
        </div>
      `;
    }

    if (agents.length > 0) {
      html += `
        <div class="prompts-sidebar-section">
          <div class="prompts-sidebar-sec-title">에이전트별 전용 지침</div>
          <div class="prompts-list-group">${agents.map(renderItem).join('')}</div>
        </div>
      `;
    }

    container.innerHTML = html;
  }

  async function selectPromptLayer(targetId, updateSidebar = true) {
    if (!targetId) return;
    activePromptId = String(targetId).trim();
    if (updateSidebar) {
      renderPromptsSidebar();
    }

    const titleEl = document.getElementById('prompt-editor-title');
    const badgeEl = document.getElementById('prompt-editor-badge');
    const scopeEl = document.getElementById('prompt-editor-scope');
    const textarea = document.getElementById('prompt-editor-textarea');
    const resetBtn = document.getElementById('btn-prompt-reset');

    if (titleEl) titleEl.textContent = '지침 불러오는 중...';

    try {
      const res = await fetch(`/api/prompts/detail?id=${encodeURIComponent(activePromptId)}`);
      const data = await res.json();
      if (!data || !data.ok || !data.detail) {
        throw new Error(data && data.error ? data.error : '지침을 불러올 수 없습니다.');
      }
      currentPromptDetail = data.detail;
      const d = currentPromptDetail;

      if (titleEl) titleEl.textContent = `${d.emoji || ''} ${d.name || d.id}`;
      if (badgeEl) {
        badgeEl.textContent = d.isCustomized ? '사용자 커스텀 활성 (런타임 오버라이드)' : '리포 기본 원본 유지';
        badgeEl.className = `prompt-tag ${d.isCustomized ? 'custom' : ''}`;
      }
      if (scopeEl) {
        scopeEl.textContent = d.injectScope || '';
      }
      if (resetBtn) {
        resetBtn.style.display = d.isCustomized ? 'inline-flex' : 'none';
      }

      if (textarea) {
        textarea.value = d.currentContent || '';
        updatePromptCounters();
      }

      // 에이전트 하위 템플릿 필드
      const childSection = document.getElementById('prompt-agent-child-section');
      const childTextarea = document.getElementById('prompt-agent-child-textarea');
      const childInjectChk = document.getElementById('prompt-agent-child-inject');

      if (d.targetType === 'agent' && childSection) {
        childSection.style.display = 'block';
        if (childTextarea) childTextarea.value = (d.childPreview && d.childPreview.childPromptTemplate) || '';
        if (childInjectChk) childInjectChk.checked = Boolean(d.childPreview && d.childPreview.injectChildTemplate !== false);
      } else if (childSection) {
        childSection.style.display = 'none';
      }

      // 조립 결과 뷰 렌더링
      renderAssembledPreview(d);
      // 기본 원본 뷰 렌더링
      renderDefaultReference(d);

    } catch (err) {
      console.warn('[PromptsCenter] 상세 로드 오류:', err);
      if (titleEl) titleEl.textContent = `로드 실패: ${err.message}`;
    }
  }

  function renderAssembledPreview(detail) {
    const fullPre = document.getElementById('prompt-assembled-full-text');
    const breakdownContainer = document.getElementById('prompt-layers-breakdown');
    const sampleInput = document.getElementById('prompt-preview-sample-task');

    if (fullPre) {
      fullPre.textContent = detail.assembledSystemPrompt || '(조립된 지침 없음)';
    }

    if (breakdownContainer && Array.isArray(detail.layers)) {
      breakdownContainer.innerHTML = detail.layers.map(layer => `
        <div class="prompt-layer-box">
          <div class="prompt-layer-box-head">
            <span>${escapeHtml(layer.title)}</span>
            <span style="font-size:11px;font-weight:normal;color:var(--text-3);">${(layer.charCount || 0).toLocaleString()}자</span>
          </div>
          <div class="prompt-layer-box-desc">${escapeHtml(layer.description || '')}</div>
          <pre class="prompt-assembly-code" style="max-height:120px;">${escapeHtml(layer.content || '')}</pre>
        </div>
      `).join('');
    }

    if (detail.childPreview && sampleInput) {
      sampleInput.value = detail.childPreview.sampleTask || sampleInput.value;
      const childPre = document.getElementById('prompt-assembled-child-text');
      if (childPre) {
        childPre.textContent = detail.childPreview.assembledChildUserPrompt || '';
      }
    }
  }

  function renderDefaultReference(detail) {
    const defPre = document.getElementById('prompt-default-ref-text');
    if (defPre) {
      defPre.textContent = detail.defaultContent || '(기본 원본 내용이 비어 있습니다)';
    }
  }

  function updatePromptCounters() {
    const textarea = document.getElementById('prompt-editor-textarea');
    const counter = document.getElementById('prompt-editor-counter');
    if (!textarea || !counter) return;

    const val = textarea.value || '';
    const charCount = val.length;
    const lineCount = val ? val.split('\n').length : 0;
    counter.textContent = `${charCount.toLocaleString()}자 · ${lineCount.toLocaleString()}줄`;
  }

  function switchPromptTab(tabName) {
    activePromptTab = tabName;
    const tabs = ['edit', 'assembled', 'default'];
    tabs.forEach(t => {
      const btn = document.getElementById(`tab-prompt-${t}`);
      const pane = document.getElementById(`pane-prompt-${t}`);
      if (btn) btn.classList.toggle('active', t === tabName);
      if (pane) pane.style.display = (t === tabName) ? 'block' : 'none';
    });
  }

  async function saveCurrentPromptLayer() {
    if (!activePromptId) return;
    const textarea = document.getElementById('prompt-editor-textarea');
    const content = textarea ? textarea.value : '';

    const payload = {
      id: activePromptId,
      content: content,
    };

    if (currentPromptDetail && currentPromptDetail.targetType === 'agent') {
      const childTextarea = document.getElementById('prompt-agent-child-textarea');
      const childInjectChk = document.getElementById('prompt-agent-child-inject');
      if (childTextarea) payload.childPromptTemplate = childTextarea.value;
      if (childInjectChk) payload.injectChildTemplate = childInjectChk.checked;
    }

    const saveBtn = document.getElementById('btn-prompt-save');
    if (saveBtn) {
      saveBtn.disabled = true;
      saveBtn.textContent = '저장 중...';
    }

    try {
      const res = await fetch('/api/prompts/save', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!data || !data.ok) {
        throw new Error(data && data.error ? data.error : '저장에 실패했습니다.');
      }

      showPromptToast('시스템 지침이 저장되었으며 런타임에 즉시 반영되었습니다.', 'success');
      await loadPromptsList();
    } catch (err) {
      alert(`지침 저장 실패: ${err.message}`);
    } finally {
      if (saveBtn) {
        saveBtn.disabled = false;
        saveBtn.textContent = '저장 및 즉시 반영';
      }
    }
  }

  async function resetCurrentPromptLayer() {
    if (!activePromptId) return;
    const name = (currentPromptDetail && currentPromptDetail.name) || activePromptId;
    if (!confirm(`정말로 "${name}"을(를) 리포지토리 초기 기본값으로 복원하시겠습니까? 커스텀 수정 내역이 초기화됩니다.`)) {
      return;
    }

    try {
      const res = await fetch('/api/prompts/reset', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: activePromptId }),
      });
      const data = await res.json();
      if (!data || !data.ok) {
        throw new Error(data && data.error ? data.error : '복원에 실패했습니다.');
      }

      showPromptToast('초기 리포지토리 기본값으로 복원되었습니다.', 'info');
      await loadPromptsList();
    } catch (err) {
      alert(`지침 복원 실패: ${err.message}`);
    }
  }

  async function refreshAssembledPreviewSample() {
    if (!activePromptId) return;
    const sampleInput = document.getElementById('prompt-preview-sample-task');
    const sample = sampleInput ? sampleInput.value : '';
    try {
      const res = await fetch(`/api/prompts/detail?id=${encodeURIComponent(activePromptId)}&sampleTask=${encodeURIComponent(sample)}`);
      const data = await res.json();
      if (data && data.ok && data.detail) {
        currentPromptDetail = data.detail;
        renderAssembledPreview(data.detail);
      }
    } catch (err) {
      console.warn('미리보기 새로고침 실패:', err);
    }
  }

  function copyAssembledPrompt() {
    const fullPre = document.getElementById('prompt-assembled-full-text');
    if (!fullPre || !fullPre.textContent) return;
    navigator.clipboard.writeText(fullPre.textContent).then(() => {
      showPromptToast('조립된 최종 시스템 프롬프트가 복사되었습니다.', 'info');
    });
  }

  function copyDefaultPrompt() {
    const defPre = document.getElementById('prompt-default-ref-text');
    if (!defPre || !defPre.textContent) return;
    navigator.clipboard.writeText(defPre.textContent).then(() => {
      showPromptToast('기본 원본 지침이 복사되었습니다.', 'info');
    });
  }

  function showPromptToast(msg, type = 'info') {
    if (typeof showToast === 'function') {
      showToast(msg, { type });
      return;
    }
    const toast = document.createElement('div');
    toast.className = `toast-pill ${type}`;
    toast.style.position = 'fixed';
    toast.style.bottom = '24px';
    toast.style.left = '50%';
    toast.style.transform = 'translateX(-50%)';
    toast.style.zIndex = '99999';
    toast.style.padding = '8px 16px';
    toast.style.borderRadius = '9999px';
    toast.style.fontSize = '13px';
    toast.style.fontWeight = '600';
    toast.style.boxShadow = 'var(--shadow-md)';
    toast.style.background = type === 'success' ? 'var(--accent)' : 'var(--text)';
    toast.style.color = '#FFFFFF';
    toast.textContent = msg;

    document.body.appendChild(toast);
    setTimeout(() => {
      toast.style.opacity = '0';
      toast.style.transition = 'opacity 0.3s ease';
      setTimeout(() => toast.remove(), 300);
    }, 2400);
  }

  // 전역 노출
  window.openPromptsCenterModal = openPromptsCenterModal;
  window.closePromptsCenterModal = closePromptsCenterModal;
  window.selectPromptLayer = selectPromptLayer;
  window.switchPromptTab = switchPromptTab;
  window.saveCurrentPromptLayer = saveCurrentPromptLayer;
  window.resetCurrentPromptLayer = resetCurrentPromptLayer;
  window.updatePromptCounters = updatePromptCounters;
  window.refreshAssembledPreviewSample = refreshAssembledPreviewSample;
  window.copyAssembledPrompt = copyAssembledPrompt;
  window.copyDefaultPrompt = copyDefaultPrompt;
})();
