// ─── 대화방 & 세션 ───
function getCurrentRoom() {
  const orchIds = (typeof ORCHESTRATOR_ROOM_IDS !== 'undefined' && Array.isArray(ORCHESTRATOR_ROOM_IDS))
    ? ORCHESTRATOR_ROOM_IDS
    : [CORE_ROOM_ID, '__expert_vault__', '__expert_coding__'];
  if (orchIds.includes(currentRoomId)) {
    ensureCoreRoom();
    return chatRooms.find(r => r && r.id === currentRoomId) || null;
  }
  return chatRooms.find(r => r && r.id === currentRoomId) || chatRooms.find(r => r && !orchIds.includes(r.id)) || chatRooms[0];
}

function getWorkdirForRoom(room) {
  if (room && room.workdir) return String(room.workdir).trim();
  if (serverStatus && (serverStatus.vaultDir || serverStatus.workdir)) return String(serverStatus.vaultDir || serverStatus.workdir).trim();
  return '';
}

function getWorkdirBasename(path) {
  if (!path) return '';
  const clean = String(path).replace(/[/\\]+$/, '');
  const parts = clean.split(/[/\\]/);
  return parts[parts.length - 1] || clean;
}

function getCliCommandForRoom(room) {
  const workdir = getWorkdirForRoom(room);
  const sid = room ? room.sessionId : null;
  return sid ? `agy --conversation ${sid} --add-dir ${workdir}` : `agy -i --add-dir ${workdir}`;
}

function copyCurrentRoomCliCommand() {
  copyToClipboard(getCliCommandForRoom(getCurrentRoom()), 'CLI 명령어가');
}

function copyCurrentRoomWorkdir() {
  const room = getCurrentRoom();
  const workdir = getWorkdirForRoom(room);
  copyToClipboard(workdir, '작업 디렉토리 경로가');
}

function updateRoomWorkdirUI() {
  const room = getCurrentRoom();
  const workdir = getWorkdirForRoom(room);
  const basename = getWorkdirBasename(workdir);

  const pillBtn = document.getElementById('btn-topbar-workdir');
  const pillName = document.getElementById('topbar-pill-workdir');
  const popoverBox = document.getElementById('popover-room-workdir');

  if (pillName) pillName.textContent = basename || 'workdir';
  if (pillBtn) {
    pillBtn.title = `작업 디렉토리(Workdir): ${workdir}\n(클릭하여 경로 복사)`;
  }
  if (popoverBox) {
    popoverBox.textContent = workdir;
    popoverBox.title = workdir;
  }
}

function focusChatInput(options = {}) {
  // isInitialLoad: 앱 첫 실행 시 모바일/터치 기기에서 키보드가 화면 절반을 가리지 않도록 건너뜀
  const isInitial = typeof options === 'boolean' ? options : !!(options && options.isInitialLoad);
  if (isInitial && isTouchOrMobileDevice()) {
    return;
  }

  const isForce = !!(options && options.force);
  // 모바일/터치 기기에서 사용자가 자동 키보드 표시 설정을 끈 경우(기본값) 포커스 건너뜀
  if (isTouchOrMobileDevice() && !isForce && !getMobileAutofocus()) {
    return;
  }

  const input = document.getElementById('chat-input');
  if (input && document.activeElement !== input) {
    try {
      input.focus({ preventScroll: true });
    } catch (_) {
      input.focus();
    }
  }
}

function syncMasterModeUi(options = {}) {
  const isMaster = applyMasterModeElements();

  if (!isMaster || options.closeSidebar) {
    toggleMasterSidebar(false);
  }
  if (isMaster && (options.loadData || !latestMasterGraph)) {
    loadMasterGraphFromServer();
    loadMasterTasksFromServer();
  }
}

function updateSessionBar(options = {}) {
  const room = getCurrentRoom();
  const btnEl = document.getElementById('btn-copy-cli-cmd');
  if (room && btnEl) {
    btnEl.title = `터미널(CLI)에서 이어하기 명령어 복사: ${getCliCommandForRoom(room)}`;
  }
  updateTopbarTitle();
  updateRoomConfigBar();
  if (!options.skipMasterUi) {
    syncMasterModeUi(options.masterUiOptions);
  }
}

function toggleRoomConfigPopover(e) {
  if (e) {
    e.preventDefault();
    e.stopPropagation();
  }
  const popover = document.getElementById('room-config-popover');
  if (!popover) return;
  if (popover.classList.contains('open')) {
    closeRoomConfigPopover();
  } else {
    openRoomConfigPopover();
  }
}

function openRoomConfigPopover() {
  const popover = document.getElementById('room-config-popover');
  const btn = document.getElementById('btn-topbar-model-config');
  if (popover) popover.classList.add('open');
  if (btn) {
    btn.classList.add('menu-open');
    btn.setAttribute('aria-expanded', 'true');
  }
  updateRoomConfigBar();
}

function closeRoomConfigPopover() {
  const popover = document.getElementById('room-config-popover');
  const btn = document.getElementById('btn-topbar-model-config');
  if (popover) popover.classList.remove('open');
  if (btn) {
    btn.classList.remove('menu-open');
    btn.setAttribute('aria-expanded', 'false');
  }
}

function formatShortModelName(modelId) {
  if (!modelId) return '';
  const m = String(modelId).toLowerCase();
  if (m.includes('flash')) return 'Flash';
  if (m.includes('hydrogen')) return 'Hydrogen 4';
  if (m.includes('orchestrator')) return 'Orchestrator';
  if (m.includes('argon')) return 'Argon 4';
  if (m.includes('sonnet')) return 'Sonnet';
  if (m.includes('opus')) return 'Opus';
  if (m.includes('gpt-oss')) return 'GPT-OSS';
  if (m.includes('haiku')) return 'Haiku';
  return modelId.length > 10 ? modelId.slice(0, 8) + '..' : modelId;
}

function getSupportedEffortsForModel(modelId) {
  const targetId = modelId || (serverStatus && serverStatus.model) || '';
  if (serverStatus && Array.isArray(serverStatus.availableModels)) {
    const found = serverStatus.availableModels.find(m => m.id === targetId);
    if (found && Array.isArray(found.supportedEfforts)) {
      return found.supportedEfforts;
    }
  }
  const m = targetId.toLowerCase();
  if (m.includes('gpt-oss')) return ['medium'];
  if (m.includes('hydrogen')) return ['low', 'medium', 'high', 'xhigh'];
  if (m.includes('orchestrator')) return [];
  if (m.includes('gemini') || m.includes('claude')) return ['low', 'medium', 'high'];
  return (serverStatus && Array.isArray(serverStatus.supportedEfforts)) ? serverStatus.supportedEfforts : ['low', 'medium', 'high'];
}

function updateRoomConfigBar() {
  const room = getCurrentRoom();
  const modelSel = document.getElementById('chat-room-model');
  const pillBtn = document.getElementById('btn-topbar-model-config');
  const pillModel = document.getElementById('topbar-pill-model');
  const pillEffort = document.getElementById('topbar-pill-effort');
  const effortDesc = document.getElementById('popover-effort-desc');
  const resetBtnWrap = document.getElementById('popover-footer-actions');
  const segBar = document.getElementById('effort-segmented-bar');
  const unsupportedNote = document.getElementById('effort-unsupported-note');

  const serverModel = (serverStatus && serverStatus.model) || 'gemini-3.8-flash';
  const serverEffort = (serverStatus && serverStatus.effort) || 'medium';

  const curModelVal = (room && room.model) ? room.model : '';
  let curEffortVal = (room && room.effort) ? room.effort : '';
  const isCustomized = Boolean(curModelVal || curEffortVal);

  const displayModel = curModelVal || serverModel;
  const supportedEfforts = getSupportedEffortsForModel(displayModel);

  // 모델 변경 등으로 현재 effort가 지원되지 않는 경우 자동 기본값 보정
  if (curEffortVal && Array.isArray(supportedEfforts) && supportedEfforts.length > 0 && !supportedEfforts.includes(curEffortVal)) {
    curEffortVal = '';
    if (room) {
      room.effort = null;
      saveChatRoomsToStorage();
      fetch('/api/chat/room/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ roomId: room.id, effort: '' })
      }).catch(() => {});
    }
  }

  // 1. 상단바 뱃지(Pill) 텍스트 및 스타일 갱신
  const displayEffort = (curEffortVal || serverEffort || 'med').toLowerCase();
  const effortShortMap = { low: 'low', medium: 'med', high: 'high', xhigh: 'xhigh', max: 'max' };

  if (pillModel) pillModel.textContent = formatShortModelName(displayModel);
  if (pillEffort) pillEffort.textContent = effortShortMap[displayEffort] || displayEffort;
  if (pillBtn) {
    pillBtn.classList.toggle('customized', isCustomized);
    const effortFull = curEffortVal ? `${curEffortVal.toUpperCase()} (방 전용)` : `${serverEffort.toUpperCase()} (기본)`;
    pillBtn.title = `대화방 AI 설정: ${displayModel} [${effortFull}] — 클릭하여 변경`;
  }

  // 2. 모델 드롭다운 옵션 갱신
  if (modelSel) {
    let modelsList = [];
    if (serverStatus && Array.isArray(serverStatus.availableModels) && serverStatus.availableModels.length > 0) {
      modelsList = serverStatus.availableModels;
    } else {
      const globalModelSel = document.getElementById('select-model');
      if (globalModelSel && globalModelSel.options.length > 0) {
        modelsList = Array.from(globalModelSel.options).map(o => ({ id: o.value, label: o.text }));
      }
    }

    const defaultModelLabel = serverModel ? `기본값 (${serverModel})` : '기본값: 서버 설정 따름';
    let modelOptionsHtml = `<option value="">${escapeHtml(defaultModelLabel)}</option>`;
    let hasSelectedModel = !curModelVal;
    for (const m of modelsList) {
      const isSel = (m.id === curModelVal);
      if (isSel) hasSelectedModel = true;
      modelOptionsHtml += `<option value="${escapeHtml(m.id)}" ${isSel ? 'selected' : ''}>${escapeHtml(m.label || m.id)}</option>`;
    }
    if (curModelVal && !hasSelectedModel) {
      modelOptionsHtml += `<option value="${escapeHtml(curModelVal)}" selected>${escapeHtml(curModelVal)}</option>`;
    }
    modelSel.innerHTML = modelOptionsHtml;
    modelSel.value = curModelVal;
  }

  // 3. Segmented 추론 강도 버튼 동적 렌더링 및 설명 갱신
  const effortDescMap = {
    '': `기본값 (${serverEffort.toUpperCase()})`,
    low: '빠른 응답 · 일상 대화',
    medium: '균형 잡힌 사고 · 일반 리서치',
    high: '심층 추론 · 정밀 설계 & 디버깅',
    xhigh: '고강도 추론 · 심화 논리 분석',
    max: '최대 추론 · 복잡한 난제 해결'
  };

  if (!supportedEfforts || supportedEfforts.length === 0) {
    if (segBar) segBar.style.display = 'none';
    if (unsupportedNote) unsupportedNote.style.display = 'block';
    if (effortDesc) effortDesc.textContent = '조절 미지원';
  } else {
    if (unsupportedNote) unsupportedNote.style.display = 'none';
    if (segBar) {
      segBar.style.display = 'flex';
      const effortMeta = {
        low: { label: 'Low', title: 'Low · 빠른 응답' },
        medium: { label: 'Med', title: 'Medium · 균형 잡힌 사고' },
        high: { label: 'High', title: 'High · 심층 추론' },
        xhigh: { label: 'X-High', title: 'X-High · 고강도 추론' },
        max: { label: 'Max', title: 'Max · 최대 추론' }
      };

      let btnsHtml = `<button type="button" class="effort-seg-btn ${!curEffortVal ? 'active' : ''}" data-effort="" onclick="onSegmentedEffortClick('')" title="서버 기본값 (${serverEffort.toUpperCase()}) 사용">기본</button>`;
      for (const eff of supportedEfforts) {
        const meta = effortMeta[eff] || { label: eff.toUpperCase(), title: eff };
        const isActive = (curEffortVal === eff);
        btnsHtml += `<button type="button" class="effort-seg-btn ${isActive ? 'active' : ''}" data-effort="${escapeHtml(eff)}" onclick="onSegmentedEffortClick('${escapeHtml(eff)}')" title="${escapeHtml(meta.title)}">${escapeHtml(meta.label)}</button>`;
      }
      segBar.innerHTML = btnsHtml;
    }
    if (effortDesc) {
      effortDesc.textContent = effortDescMap[curEffortVal] || curEffortVal.toUpperCase();
    }
  }

  // 4. 기본값 복원 버튼 노출 여부
  if (resetBtnWrap) {
    resetBtnWrap.style.display = isCustomized ? 'flex' : 'none';
  }

  // 5. 작업 디렉토리(Workdir) 배지 및 팝오버 갱신
  updateRoomWorkdirUI();
}

async function onSegmentedEffortClick(eff) {
  await onRoomEffortChange(eff);
}

async function resetRoomConfigToDefault() {
  const room = getCurrentRoom();
  if (!room) return;
  room.model = null;
  room.effort = null;
  saveChatRoomsToStorage();
  updateRoomConfigBar();
  try {
    await fetch('/api/chat/room/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ roomId: room.id, model: '', effort: '' })
    });
    showToast('대화방 설정을 서버 기본값으로 복원했습니다.');
  } catch (err) {
    console.error('설정 복원 실패:', err);
  }
}

async function onRoomModelChange(newModel) {
  const room = getCurrentRoom();
  if (!room) return;
  const cleanVal = (newModel && String(newModel).trim()) ? String(newModel).trim() : null;
  room.model = cleanVal;

  // 새 모델이 지원하지 않는 effort인 경우 effort도 자동 초기화
  const supported = getSupportedEffortsForModel(cleanVal);
  if (room.effort && Array.isArray(supported) && !supported.includes(room.effort)) {
    room.effort = null;
  }

  saveChatRoomsToStorage();
  updateRoomConfigBar();
  try {
    const res = await fetch('/api/chat/room/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ roomId: room.id, model: cleanVal || '', effort: room.effort || '' })
    });
    const data = await res.json();
    if (data.ok) {
      showToast(cleanVal ? `대화방 모델: ${cleanVal}` : '대화방 모델: 기본값으로 복원');
    }
  } catch (err) {
    console.error('방 모델 설정 저장 실패:', err);
  }
}

async function onRoomEffortChange(newEffort) {
  const room = getCurrentRoom();
  if (!room) return;
  const cleanVal = (newEffort && String(newEffort).trim()) ? String(newEffort).trim() : null;
  room.effort = cleanVal;
  saveChatRoomsToStorage();
  updateRoomConfigBar();
  try {
    const res = await fetch('/api/chat/room/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ roomId: room.id, effort: cleanVal || '' })
    });
    const data = await res.json();
    if (data.ok) {
      showToast(cleanVal ? `대화방 추론 강도: ${cleanVal.toUpperCase()}` : '대화방 추론 강도: 기본값으로 복원');
    }
  } catch (err) {
    console.error('방 추론 강도 설정 저장 실패:', err);
  }
}

function createNewChat(force = false, options = {}) {
  if (currentView !== 'chat') switchView('chat');
  const cur = getCurrentRoom();
  if (!force && cur && (!cur.messages || cur.messages.length === 0) && !cur.sessionId) {
    focusChatInput();
    selectRoom(cur.id, options);
    return;
  }
  // 새 대화를 생성하기 전 남아있는 다른 미사용 빈 방 정리
  cleanUnusedEmptyRooms();
  const now = Date.now();
  const newId = 'room-' + now;
  chatRooms.unshift({
    id: newId,
    title: '새 대화',
    messages: [],
    isNewSession: true,
    userCreatedEmpty: true,
    sessionId: null,
    updatedAt: now,
    createdAt: now
  });
  sortChatRooms();
  saveChatRoomsToStorage();
  focusChatInput();
  selectRoom(newId, options);
}

async function selectRoom(roomId, options = {}) {
  const { keepDrawerOpen = false, historyMode = 'push' } = (options || {});
  const prevRoomId = currentRoomId;
  if (currentView !== 'chat') switchView('chat');
  // 방금 띄운 빈 새 대화방에서 아무런 말도 하지 않고 다른 대화방으로 이동할 때 목록에서 제거
  cleanUnusedEmptyRooms(roomId);
  currentRoomId = roomId;

  // [낙관적 UI] 열람한 대화방의 읽음(unread) 및 연결된 태스크 검토(reviewed) 즉시 해제
  const targetRoom = getRoomById(roomId);
  if (targetRoom) {
    targetRoom.unread = false;
  }
  if (Array.isArray(latestMasterTasks)) {
    const tasksToReview = latestMasterTasks.filter(t => t && t.roomId === roomId && !t.reviewed);
    tasksToReview.forEach(t => {
      t.reviewed = true;
      t.reviewedAt = Date.now() / 1000;
      // 서버 동기화는 백그라운드로 안전하게 수행
      if (t.id) {
        fetch('/api/orchestrator/task/update', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ taskId: t.id, reviewed: true })
        }).catch(err => console.warn('태스크 검토 서버 동기화 실패:', err));
      }
    });
    if (tasksToReview.length > 0 && typeof updateMasterTopbarBadge === 'function') {
      updateMasterTopbarBadge();
    }
  }
  if (typeof syncRoomHistory === 'function') {
    const effectiveMode = (historyMode === 'push' && prevRoomId === roomId) ? 'replace' : historyMode;
    syncRoomHistory(roomId, effectiveMode);
  }
  saveChatRoomsToStorage();
  renderRoomList();
  updateSessionBar({ skipMasterUi: true });
  focusChatInput();

  syncMasterModeUi({ closeSidebar: true, loadData: true });

  // 이전 방의 메시지가 잔상으로 남지 않도록 현재 방 상태(빈 대화창 또는 기존 메시지)로 즉시 렌더링
  renderMessagesForCurrentRoom(true);
  updateSendButtonState();

  const room = getRoomById(roomId);
  if (room && !room.userCreatedEmpty) {
    await loadChatHistoryFromServer(room.id, room.sessionId);
  }

  const liveState = ensureRoomLiveState(roomId);
  if (liveState.isRunning) {
    setOrbState('thinking', liveState.feed || '생각하는 중…');
  } else if (!isVoiceRecording) {
    setOrbState('idle', '준비 완료');
  }
  if (!keepDrawerOpen) {
    closeRoomDrawer();
  }
  focusChatInput();
}

function getDateGroupLabel(ts) {
  const ms = getRoomTimestamp({ updatedAt: ts });
  if (!ms) return '이전';
  const now = new Date();
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const diffDays = Math.floor((startOfToday - ms) / 86400000);
  if (ms >= startOfToday) return '오늘';
  if (diffDays === 0) return '어제';
  if (diffDays < 7) return '최근 7일';
  return '이전';
}

let openSwipedRoomItem = null;

function closeSwipedRoom() {
  if (openSwipedRoomItem) {
    openSwipedRoomItem.classList.remove('swiped-open');
    openSwipedRoomItem.closest('.room-item-wrap')?.classList.remove('swipe-active');
    openSwipedRoomItem.style.transform = '';
    openSwipedRoomItem = null;
    return true;
  }
  return false;
}

function onRoomItemClick(event, roomId) {
  if (awaitingRoomLongPressRelease || Date.now() < suppressRoomClickUntil || suppressNextRoomClick) {
    suppressNextRoomClick = false;
    if (event && typeof event.preventDefault === 'function') event.preventDefault();
    if (event && typeof event.stopPropagation === 'function') event.stopPropagation();
    return;
  }
  if (closeSwipedRoom()) {
    if (event && typeof event.preventDefault === 'function') event.preventDefault();
    if (event && typeof event.stopPropagation === 'function') event.stopPropagation();
    return;
  }
  if (isRoomSelectionMode || (event && (event.metaKey || event.ctrlKey || event.shiftKey))) {
    toggleRoomSelection(roomId, event);
    return;
  }
  selectRoom(roomId);
}

function isMasterSubtaskRoom(r) {
  if (!r || !r.id || ORCHESTRATOR_ROOM_IDS.includes(r.id)) return false;
  const pid = String(r.parentAgentId || '').trim();
  if (pid && ORCHESTRATOR_ROOM_IDS.includes(pid)) return true;
  if (r.isOrchestratorChild || r.channel === 'orchestrator') return true;
  if (Array.isArray(r.messages) && r.messages.some(m => m && m.channel === 'orchestrator')) return true;
  if (Array.isArray(latestMasterTasks) && latestMasterTasks.some(t => t && t.roomId === r.id)) return true;
  if (latestMasterGraph && Array.isArray(latestMasterGraph.nodes)) {
    const node = latestMasterGraph.nodes.find(n => n && n.id === r.id);
    if (node && node.dispatched) return true;
  }
  const live = roomLiveStates[r.id];
  if (live && live.channel === 'orchestrator') return true;
  return false;
}

function renderMasterSubtaskTree(masterChildRooms) {
  const renderTree = (containerId, rooms, fallbackLabel = '직속 채팅') => {
    const treeEl = document.getElementById(containerId);
    if (!treeEl) return;
    if (!rooms || rooms.length === 0) {
      treeEl.innerHTML = '';
      return;
    }
    treeEl.innerHTML = rooms.slice(0, 30).map(r => {
      const safeId = escapeHtml(r.id);
      const live = roomLiveStates[r.id];
      const isRunning = Boolean(live && live.isRunning);
      const relTime = formatRelativeTime(r.updatedAt || r.createdAt);
      const isCur = (r.id === currentRoomId);
      const hasPendingTask = !isCur && Array.isArray(latestMasterTasks) && latestMasterTasks.some(t => t && t.roomId === r.id && isPendingMasterTask(t));
      const isUnread = !isCur && Boolean(r.unread || hasPendingTask);
      const cls = [
        'sb-tree-item',
        r.id === currentRoomId ? 'active' : '',
        isRunning ? 'running' : '',
        isUnread ? 'unread' : '',
      ].filter(Boolean).join(' ');
      return `
        <div class="${cls}" data-room-id="${safeId}" onclick="onRoomItemClick(event, '${safeId}')" title="${escapeHtml(r.title || fallbackLabel)}">
          <svg class="i sb-tree-icon" viewBox="0 0 24 24"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
          <span class="sb-tree-title">${escapeHtml(r.title || fallbackLabel)}</span>
          ${relTime ? `<span class="sb-tree-time">${escapeHtml(relTime)}</span>` : ''}
          ${isRunning ? '<span class="run-label">실행 중</span><span class="run-spinner" role="status" aria-label="실행 중"></span>' : (isUnread ? '<span class="sb-tree-dot"></span>' : '')}
          <button type="button" class="sb-tree-del-btn" onclick="event.stopPropagation();deleteRoom(event, '${safeId}')" title="삭제">✕</button>
        </div>
      `;
    }).join('');
  };

  const masterRooms = masterChildRooms.filter(r => !r.parentAgentId || r.parentAgentId === CORE_ROOM_ID);
  const vaultRooms = masterChildRooms.filter(r => r.parentAgentId === EXPERT_VAULT_ROOM_ID);
  const codingRooms = masterChildRooms.filter(r => r.parentAgentId === EXPERT_CODING_ROOM_ID);

  renderTree('master-subtask-tree', masterRooms, '마스터 직속 채팅');
  renderTree('expert-vault-subtask-tree', vaultRooms, '세컨브레인 직속 채팅');
  renderTree('expert-coding-subtask-tree', codingRooms, '봇개발 직속 채팅');
}

function renderRoomList() {
  updateCoreAgentSlotUi();
  const uniqueRooms = getFilteredUniqueRooms();
  const activeRooms = uniqueRooms.filter(r => !r.isArchived);
  const archivedRooms = uniqueRooms.filter(r => Boolean(r.isArchived));

  const masterChildRooms = activeRooms.filter(isMasterSubtaskRoom);
  const generalRooms = activeRooms.filter(r => !isMasterSubtaskRoom(r));
  renderMasterSubtaskTree(masterChildRooms);

  const listPc = document.getElementById('room-list-pc');
  if (listPc) {
    if (generalRooms.length === 0) {
      const emptyMsg = roomSearchQuery.trim() ? '검색 결과가 없습니다.' : (masterChildRooms.length > 0 ? '일반 대화가 없습니다.' : '대화가 없습니다.');
      listPc.innerHTML = `
        <div class="sb-general-header">
          <div class="sb-general-header-left">
            <span>일반 채팅</span>
            <button type="button" class="new-chat-inline-btn" onclick="createNewChat()" title="새 일반 대화 시작 (⇧⌘O)">
              <svg class="i" viewBox="0 0 24 24"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>
              <span>새 대화</span>
            </button>
          </div>
        </div>
        <div class="sb-empty">${emptyMsg}</div>
      `;
    } else {
      let lastGroup = '';
      const html = [
        '<div class="sb-general-header">' +
          '<div class="sb-general-header-left">' +
            '<span>일반 채팅</span>' +
            '<button type="button" class="new-chat-inline-btn" onclick="createNewChat()" title="새 일반 대화 시작 (⇧⌘O)">' +
              '<svg class="i" viewBox="0 0 24 24"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>' +
              '<span>새 대화</span>' +
              '<kbd class="desktop-only" id="kbd-new-chat">⇧⌘O</kbd>' +
            '</button>' +
          '</div>' +
          '<span>최근 사용순</span>' +
        '</div>'
      ];
      for (const r of generalRooms) {
        const group = r.pinned ? '고정됨' : getDateGroupLabel(r.updatedAt || r.createdAt);
        if (group !== lastGroup) {
          lastGroup = group;
          html.push(`<div class="sb-group-label"><span>${group}</span></div>`);
        }
        const live = roomLiveStates[r.id];
        const isRunning = Boolean(live && live.isRunning);
        const queuedInRoom = getQueuedMessagesCount(r);
        const isSelected = selectedRoomIds.has(String(r.id));
        const safeId = escapeHtml(r.id);
        const relTime = formatRelativeTime(r.updatedAt || r.createdAt);
        const isCur = (r.id === currentRoomId);
        const hasPendingTask = !isCur && Array.isArray(latestMasterTasks) && latestMasterTasks.some(t => t && t.roomId === r.id && isPendingMasterTask(t));
        const isUnread = !isCur && Boolean(r.unread || hasPendingTask);

        html.push(`
          <div class="room-item-wrap" data-room-id="${safeId}">
            <div class="room-swipe-actions">
              <button type="button" class="room-swipe-del-btn" onclick="event.stopPropagation();deleteRoom(event, '${safeId}')" title="대화 삭제">
                ${ICONS.trash}
                <span>삭제</span>
              </button>
            </div>
            <div class="room-item ${r.id === currentRoomId ? 'active' : ''} ${isRunning ? 'running' : ''} ${isSelected ? 'selected' : ''}"
                 data-room-id="${safeId}"
                 onclick="onRoomItemClick(event, '${safeId}')">
              <span class="room-check" onclick="toggleRoomSelection('${safeId}', event)">${ICONS.check}</span>
              ${isRunning ? '<span class="run-spinner" role="status" aria-label="실행 중"></span>' : ''}
              <div class="room-body">
                <div class="room-row-top">
                  <span class="room-title">${escapeHtml(r.title || '새 대화')}</span>
                  ${queuedInRoom > 0 ? `<span class="room-queue-badge">대기 ${queuedInRoom}</span>` : ''}
                  ${relTime ? `<span class="room-time">${escapeHtml(relTime)}</span>` : ''}
                  ${isUnread ? '<span class="sb-tree-dot" title="확인 필요"></span>' : ''}
                </div>
                ${r._searchSnippet ? `<div class="room-search-snippet">${escapeHtml(r._searchSnippet)}</div>` : ''}
              </div>
              <div class="room-actions-inline">
                <button type="button" class="room-del" onclick="event.stopPropagation();toggleArchiveRoom('${safeId}', true)" title="대화 보관"><svg class="i" viewBox="0 0 24 24" style="width:13px;height:13px;"><polyline points="21 8 21 21 3 21 3 8"/><rect x="1" y="3" width="22" height="5"/><line x1="10" y1="12" x2="14" y2="12"/></svg></button>
                <button type="button" class="room-del" onclick="event.stopPropagation();openRenameRoomModal('${safeId}')" title="이름 변경">${ICONS.edit}</button>
                <button type="button" class="room-del" onclick="deleteRoom(event, '${safeId}')" title="삭제">${ICONS.trash}</button>
              </div>
            </div>
          </div>
        `);
      }
      listPc.innerHTML = html.join('');
    }
  }

  // 보관된 대화 아코디언 렌더링
  const secArchived = document.getElementById('sb-archived-section');
  const listArchived = document.getElementById('sb-archived-list');
  const countArchived = document.getElementById('sb-archived-count');
  if (secArchived && listArchived && countArchived) {
    if (archivedRooms.length === 0) {
      secArchived.style.display = 'none';
    } else {
      secArchived.style.display = 'block';
      countArchived.innerText = String(archivedRooms.length);
      listArchived.innerHTML = archivedRooms.map(r => {
        const safeId = escapeHtml(r.id);
        const relTime = formatRelativeTime(r.updatedAt || r.createdAt);
        return `
          <div class="room-item-wrap" data-room-id="${safeId}">
            <div class="room-swipe-actions">
              <button type="button" class="room-swipe-del-btn" onclick="event.stopPropagation();deleteRoom(event, '${safeId}')" title="대화 삭제">
                ${ICONS.trash}
                <span>삭제</span>
              </button>
            </div>
            <div class="room-item ${r.id === currentRoomId ? 'active' : ''}" style="padding:6px 10px;min-height:38px;" data-room-id="${safeId}" onclick="onRoomItemClick(event, '${safeId}')">
              <div class="room-body">
                <div class="room-row-top">
                  <span class="room-title" style="font-size:12.5px;color:var(--text-2);">${escapeHtml(r.title || '보관된 대화')}</span>
                  ${relTime ? `<span class="room-time" style="font-size:10.5px;">${escapeHtml(relTime)}</span>` : ''}
                </div>
              </div>
              <div class="room-actions-inline">
                <button type="button" class="room-del" onclick="event.stopPropagation();toggleArchiveRoom('${safeId}', false)" title="보관 해제">↩️</button>
                <button type="button" class="room-del" onclick="deleteRoom(event, '${safeId}')" title="삭제">${ICONS.trash}</button>
              </div>
            </div>
          </div>
        `;
      }).join('');
    }
  }

  if (isRoomSelectionMode) updateRoomSelectionUi();
  updateTopbarTitle();
}

function bindRoomLongPressEvents() {
  const sidebarEl = document.getElementById('room-sidebar') || document.getElementById('room-list-pc');
  if (!sidebarEl || sidebarEl._gesturesBound) return;
  sidebarEl._gesturesBound = true;

  let pressTimer = null;
  let visualTimer = null;
  let startX = 0;
  let startY = 0;
  let targetItem = null;
  let lastTouchTime = 0;
  let isSwiping = false;
  let swipeDirectionDecided = false;
  let hasSwiped = false;
  let isDraggingMouse = false;

  const clearTimers = () => {
    if (pressTimer) { clearTimeout(pressTimer); pressTimer = null; }
    if (visualTimer) { clearTimeout(visualTimer); visualTimer = null; }
    if (targetItem && !isSwiping) { targetItem.classList.remove('pressing'); }
  };

  const resetClickSuppression = () => {
    awaitingRoomLongPressRelease = false;
    suppressRoomClickUntil = 0;
    suppressNextRoomClick = false;
  };

  const finishLongPressRelease = () => {
    if (awaitingRoomLongPressRelease) {
      awaitingRoomLongPressRelease = false;
      suppressRoomClickUntil = Date.now() + 600;
    }
  };

  const startGesture = (itemEl, clientX, clientY, isMouse = false) => {
    clearTimers();
    const roomId = itemEl.getAttribute('data-room-id');
    if (!roomId) return;

    // 다른 아이템이 열려있다면 닫기
    if (openSwipedRoomItem && openSwipedRoomItem !== itemEl) {
      closeSwipedRoom();
      suppressNextRoomClick = true;
      setTimeout(() => { suppressNextRoomClick = false; }, 250);
    }

    targetItem = itemEl;
    startX = clientX;
    startY = clientY;
    isSwiping = false;
    swipeDirectionDecided = false;
    hasSwiped = false;
    isDraggingMouse = isMouse;

    if (!isRoomSelectionMode) {
      visualTimer = setTimeout(() => {
        if (targetItem && !isSwiping) targetItem.classList.add('pressing');
    }, 260);

      pressTimer = setTimeout(() => {
        if (!targetItem || isSwiping) return;
        targetItem.classList.remove('pressing');
        awaitingRoomLongPressRelease = true;
        lastRoomLongPressTime = Date.now();
        suppressRoomClickUntil = Date.now() + 2000;
        suppressNextRoomClick = true;
        enterRoomSelectionMode(roomId);
        targetItem = null;
      }, 440);
    }
  };

  const moveGesture = (clientX, clientY) => {
    if (!targetItem) return;
    const dx = clientX - startX;
    const dy = clientY - startY;

    if (isRoomSelectionMode) {
      if (Math.abs(dx) > 10 || Math.abs(dy) > 10) clearTimers();
      return;
    }

    if (!swipeDirectionDecided) {
      if (Math.abs(dy) > 8 && Math.abs(dy) >= Math.abs(dx)) {
        swipeDirectionDecided = true;
        clearTimers();
        targetItem = null;
        return;
      }
      if (Math.abs(dx) > 8 && Math.abs(dx) > Math.abs(dy)) {
        swipeDirectionDecided = true;
        isSwiping = true;
        hasSwiped = true;
        clearTimers();
        targetItem.classList.remove('pressing');
        targetItem.classList.add('swiping');
        targetItem.closest('.room-item-wrap')?.classList.add('swipe-active');
      }
    }

    if (isSwiping && targetItem) {
      const isAlreadyOpen = targetItem.classList.contains('swiped-open');
      let currentOffset = isAlreadyOpen ? -72 + dx : dx;
      if (currentOffset > 0) currentOffset = Math.min(10, currentOffset * 0.2);
      if (currentOffset < -72) currentOffset = -72 + (currentOffset + 72) * 0.25;
      targetItem.style.transform = `translateX(${currentOffset}px)`;
    }
  };

  const endGesture = (clientX) => {
    clearTimers();
    if (targetItem) {
      targetItem.classList.remove('pressing');
      if (isSwiping) {
        targetItem.classList.remove('swiping');
        const dx = clientX - startX;
        const isAlreadyOpen = targetItem.classList.contains('swiped-open');

        if (isAlreadyOpen) {
          if (dx > 20) {
            targetItem.classList.remove('swiped-open');
            targetItem.closest('.room-item-wrap')?.classList.remove('swipe-active');
            targetItem.style.transform = '';
            if (openSwipedRoomItem === targetItem) openSwipedRoomItem = null;
          } else {
            targetItem.classList.add('swiped-open');
            targetItem.closest('.room-item-wrap')?.classList.add('swipe-active');
            targetItem.style.transform = '';
            openSwipedRoomItem = targetItem;
          }
        } else {
          if (dx < -32) {
            targetItem.classList.add('swiped-open');
            targetItem.closest('.room-item-wrap')?.classList.add('swipe-active');
            targetItem.style.transform = '';
            openSwipedRoomItem = targetItem;
          } else {
            targetItem.classList.remove('swiped-open');
            targetItem.closest('.room-item-wrap')?.classList.remove('swipe-active');
            targetItem.style.transform = '';
          }
        }
        suppressNextRoomClick = true;
        suppressRoomClickUntil = Date.now() + 350;
        setTimeout(() => { suppressNextRoomClick = false; }, 350);
      }
    }
    finishLongPressRelease();
    targetItem = null;
    isSwiping = false;
    swipeDirectionDecided = false;
    isDraggingMouse = false;
  };

  sidebarEl.addEventListener('touchstart', (e) => {
    if (!e.touches || e.touches.length !== 1) return;
    lastTouchTime = Date.now();
    resetClickSuppression();
    if (e.target.closest('.room-actions-inline') || e.target.closest('.room-check') || e.target.closest('.room-swipe-del-btn')) return;
    const item = e.target.closest('.room-item');
    if (!item) return;

    if (isEditableElement(document.activeElement)) {
      try { document.activeElement.blur(); } catch (_) {}
    }
    startGesture(item, e.touches[0].clientX, e.touches[0].clientY, false);
  }, { passive: true });

  sidebarEl.addEventListener('touchmove', (e) => {
    if (!e.touches || !e.touches[0]) return;
    moveGesture(e.touches[0].clientX, e.touches[0].clientY);
  }, { passive: true });

  sidebarEl.addEventListener('touchend', (e) => {
    lastTouchTime = Date.now();
    const clientX = (e.changedTouches && e.changedTouches[0]) ? e.changedTouches[0].clientX : startX;
    endGesture(clientX);
  }, { passive: true });

  sidebarEl.addEventListener('touchcancel', () => {
    lastTouchTime = Date.now();
    clearTimers();
    if (targetItem) {
      targetItem.classList.remove('pressing', 'swiping');
      if (!targetItem.classList.contains('swiped-open')) {
        targetItem.closest('.room-item-wrap')?.classList.remove('swipe-active');
      }
      targetItem.style.transform = '';
      targetItem = null;
    }
    finishLongPressRelease();
    isSwiping = false;
  }, { passive: true });

  sidebarEl.addEventListener('mousedown', (e) => {
    if (e.button !== 0) return;
    if (Date.now() - lastTouchTime < 600) return;
    resetClickSuppression();
    if (e.target.closest('.room-actions-inline') || e.target.closest('.room-check') || e.target.closest('.room-swipe-del-btn')) return;
    const item = e.target.closest('.room-item');
    if (!item) return;
    startGesture(item, e.clientX, e.clientY, true);
  });

  window.addEventListener('mousemove', (e) => {
    if (!isDraggingMouse) return;
    moveGesture(e.clientX, e.clientY);
  });

  window.addEventListener('mouseup', (e) => {
    if (!isDraggingMouse) return;
    endGesture(e.clientX);
  });

  sidebarEl.addEventListener('contextmenu', (e) => {
    const item = e.target.closest('.room-item');
    if (!item) return;
    e.preventDefault();
    clearTimers();
    if (
      awaitingRoomLongPressRelease ||
      Date.now() < suppressRoomClickUntil ||
      (Date.now() - lastRoomLongPressTime < 1200) ||
      (Date.now() - lastTouchTime < 1000)
    ) {
      return;
    }
    const roomId = item.getAttribute('data-room-id');
    if (!roomId) return;
    if (!isRoomSelectionMode) enterRoomSelectionMode(roomId);
    else toggleRoomSelection(roomId);
  });

  if (!window._roomSwipeDocListenerBound) {
    window._roomSwipeDocListenerBound = true;
    document.addEventListener('click', (e) => {
      if (openSwipedRoomItem && !e.target.closest('.room-item-wrap')) {
        closeSwipedRoom();
      }
    }, true);
  }
}
