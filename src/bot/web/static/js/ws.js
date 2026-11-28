// ─── WebSocket ───
let wsInitialConnected = false;
const pendingChatQueue = [];
function connectWs() {
  if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
    return;
  }
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const token = getGatewayToken();
  const wsUrl = `${protocol}//${location.host}/ws` + (token ? `?token=${encodeURIComponent(token)}` : '');
  socket = new WebSocket(wsUrl);

  socket.onopen = () => {
    if (typeof addBreadcrumb === 'function') addBreadcrumb('ws', 'WebSocket 연결 수립');
    const banner = document.getElementById('conn-banner');
    if (banner) banner.classList.remove('show');
    if (wsInitialConnected) {
      loadChatRoomsFromServer(false, true);
    }
    wsInitialConnected = true;
    const curRoom = getCurrentRoom();
    const curLive = curRoom ? roomLiveStates[curRoom.id] : null;
    if (curLive && curLive.isRunning) {
      setOrbState('thinking', curLive.feed || '생각하는 중…');
    } else {
      setOrbState('idle', '준비 완료');
    }
    while (pendingChatQueue.length > 0 && socket && socket.readyState === WebSocket.OPEN) {
      const queued = pendingChatQueue.shift();
      sendUserMessage(queued.text, queued.isVoiceTurn);
    }
  };

  socket.onmessage = (event) => {
    try {
      handleWsEvent(JSON.parse(event.data));
    } catch (_) {}
  };

  socket.onclose = (e) => {
    if (typeof addBreadcrumb === 'function') addBreadcrumb('ws_close', `소켓 연결 해제 (code=${e?.code || 'unknown'}, clean=${e?.wasClean})`);
    const banner = document.getElementById('conn-banner');
    if (banner) banner.classList.add('show');
    setAgentAvatarStatus('offline', '재연결 중…');
    setTimeout(connectWs, 2000);
  };

  socket.onerror = () => {
    if (typeof addBreadcrumb === 'function') addBreadcrumb('ws_error', '소켓 오류 발생');
  };
}

function setOrbState(state, label) {
  const orb = document.getElementById('voice-orb');
  if (orb) {
    orb.className = 'state-dot ' + (state || 'idle');
    if (label) orb.title = label;
  }
  const liveFeedEl = document.getElementById('live-feed-text');
  if (liveFeedEl && label && state === 'thinking') {
    liveFeedEl.innerText = label;
  }
  updateComposerStatusBar(state, label);
}

function getQueuedMessagesCount(room) {
  if (!room || !Array.isArray(room.messages)) return 0;
  return room.messages.filter(m => m && m.queued).length;
}

function insertMessageBeforeQueued(room, msgObj) {
  if (!room) return;
  if (!Array.isArray(room.messages)) room.messages = [];
  const firstQueuedIdx = room.messages.findIndex(m => m && m.queued);
  if (firstQueuedIdx !== -1) {
    room.messages.splice(firstQueuedIdx, 0, msgObj);
  } else {
    room.messages.push(msgObj);
  }
}

function upsertRoomMessage(room, msgObj, options = {}) {
  if (!room || !msgObj || typeof msgObj !== 'object') return null;
  if (!Array.isArray(room.messages)) room.messages = [];
  room._mutatedSeq = (room._mutatedSeq || 0) + 1;

  const incId = (msgObj.id !== undefined && msgObj.id !== null && msgObj.id !== '') ? msgObj.id : null;
  const incClientId = msgObj.clientMsgId ? String(msgObj.clientMsgId).trim() : null;
  const incRole = msgObj.sender || msgObj.role || 'assistant';
  const incText = String(msgObj.text || '').trim();

  let matchIdx = -1;
  if (incId !== null) {
    matchIdx = room.messages.findIndex(m => m && m.id === incId);
  }
  if (matchIdx === -1 && incClientId) {
    matchIdx = room.messages.findIndex(m => m && m.clientMsgId === incClientId);
  }
  if (matchIdx === -1 && incRole !== 'assistant' && incText) {
    for (let i = room.messages.length - 1; i >= 0; i--) {
      const m = room.messages[i];
      if (!m) continue;
      const mRole = m.sender || m.role || 'assistant';
      if (mRole !== incRole) continue;
      const mHasId = (m.id !== undefined && m.id !== null && m.id !== '');
      const sameUnidentified = (!mHasId && !m.clientMsgId) || (incId !== null && !mHasId) || Boolean(m.queued && !incClientId);
      if (sameUnidentified && String(m.text || '').trim() === incText) {
        matchIdx = i;
        break;
      }
    }
  }

  if (matchIdx !== -1) {
    const existing = room.messages[matchIdx];
    if (incId !== null) existing.id = incId;
    if (incClientId) existing.clientMsgId = incClientId;
    if (incText && (!String(existing.text || '').trim() || incText.length >= String(existing.text || '').trim().length)) {
      existing.text = msgObj.text;
    }
    if (msgObj.channel) existing.channel = msgObj.channel;
    if (msgObj.role) existing.role = msgObj.role;
    if (msgObj.sender) existing.sender = msgObj.sender;
    if (Array.isArray(msgObj.steps) && msgObj.steps.length > 0) existing.steps = msgObj.steps;
    if (Array.isArray(msgObj.attachments) && msgObj.attachments.length > 0) existing.attachments = msgObj.attachments;
    if (msgObj.model) existing.model = msgObj.model;
    if (msgObj.effort !== undefined && msgObj.effort !== null) existing.effort = msgObj.effort;
    if (msgObj.elapsedSeconds !== undefined) existing.elapsedSeconds = msgObj.elapsedSeconds;
    if (msgObj.isError !== undefined) existing.isError = msgObj.isError;
    if (msgObj.isSlotPlaceholder !== undefined) existing.isSlotPlaceholder = msgObj.isSlotPlaceholder;
    if (msgObj.slotTaskId) existing.slotTaskId = msgObj.slotTaskId;
    if (msgObj.bypassedFromRoomId) existing.bypassedFromRoomId = msgObj.bypassedFromRoomId;
    if (msgObj.agentName) existing.agentName = msgObj.agentName;
    if (msgObj.subagentTitle) existing.subagentTitle = msgObj.subagentTitle;
    if (Array.isArray(msgObj.delegatedRooms) && msgObj.delegatedRooms.length > 0) {
      existing.delegatedRooms = msgObj.delegatedRooms;
    }
    if (msgObj.createdAt && (!existing.createdAt || options.updateTimestamp)) {
      existing.createdAt = msgObj.createdAt;
    }

    if (options.clearQueued) {
      delete existing.queued;
      delete existing.queuePos;
      delete existing._localPendingAt;
    } else if (msgObj.queued === true) {
      existing.queued = true;
      if (msgObj.queuePos) existing.queuePos = msgObj.queuePos;
    }

    if (options.beforeQueued && !existing.queued) {
      const firstQueuedIdx = room.messages.findIndex(m => m && m.queued);
      if (firstQueuedIdx !== -1 && matchIdx > firstQueuedIdx) {
        room.messages.splice(matchIdx, 1);
        room.messages.splice(firstQueuedIdx, 0, existing);
      }
    }
    return existing;
  }

  const newMsg = { ...msgObj };
  if (incId === null) delete newMsg.id;
  if (!incClientId) delete newMsg.clientMsgId;
  if (options.beforeQueued && !newMsg.queued) {
    insertMessageBeforeQueued(room, newMsg);
  } else {
    room.messages.push(newMsg);
  }
  return newMsg;
}

function renumberQueuedMessages(room) {
  if (!room || !Array.isArray(room.messages)) return 0;
  let pos = 1;
  room.messages.forEach(m => {
    if (m && m.queued) m.queuePos = pos++;
  });
  return pos - 1;
}

function updateComposerStatusBar(state, label) {
  const bar = document.getElementById('composer-status-bar');
  const content = document.getElementById('composer-status-content');
  const actionBtn = document.getElementById('composer-status-action');
  if (!bar || !content || !actionBtn) return;

  const curRoom = getCurrentRoom();
  const queuedCount = getQueuedMessagesCount(curRoom);

  if (isVoiceRecording || state === 'listening') {
    bar.classList.add('show');
    bar.classList.remove('queued-mode');
    content.innerHTML = `<span class="voice-wave"><span></span><span></span><span></span><span></span></span><span>${escapeHtml(label || '음성 인식 중… 말씀이 끝나면 자동 전송됩니다')}</span>`;
    actionBtn.innerText = '완료';
    actionBtn.style.display = 'inline-flex';
  } else if (state === 'speaking' || (currentAudio && !currentAudio.paused)) {
    bar.classList.add('show');
    bar.classList.remove('queued-mode');
    const qSuffix = queuedCount > 0 ? ` · 대기열 ${queuedCount}건 대기 중` : '';
    content.innerHTML = `${ICONS.volume}<span>${escapeHtml((label || '음성으로 답변을 읽는 중…') + qSuffix)}</span>`;
    actionBtn.innerText = '중지';
    actionBtn.style.display = 'inline-flex';
  } else if (queuedCount > 0) {
    bar.classList.add('show');
    bar.classList.add('queued-mode');
    content.innerHTML = `<span class="queue-pulse-dot"></span><span>대기열 <strong>${queuedCount}건</strong> 대기 중 · 현재 답변 완료 후 순차 실행됩니다</span>`;
    actionBtn.innerText = '대기 취소';
    actionBtn.style.display = 'inline-flex';
  } else {
    bar.classList.remove('show');
    bar.classList.remove('queued-mode');
  }
}

function onComposerStatusAction() {
  if (isVoiceRecording) {
    if (recognition) {
      try { recognition.stop(); } catch (_) { stopVoiceRecording(true); }
    } else {
      stopVoiceRecording(true);
    }
    return;
  }
  if (currentAudio) {
    try { currentAudio.pause(); } catch (_) {}
    currentAudio = null;
    const curRoom = getCurrentRoom();
    const curLive = curRoom ? roomLiveStates[curRoom.id] : null;
    if (curLive && curLive.isRunning) {
      setOrbState('thinking', curLive.feed || '생각하는 중…');
    } else {
      setOrbState('idle', '준비 완료');
    }
    return;
  }
  const curRoom = getCurrentRoom();
  if (getQueuedMessagesCount(curRoom) > 0) {
    cancelAllQueuedMessages(curRoom.id);
  }
}

function getRoomById(roomId) {
  if (!roomId) return null;
  const orchIds = (typeof ORCHESTRATOR_ROOM_IDS !== 'undefined' && Array.isArray(ORCHESTRATOR_ROOM_IDS))
    ? ORCHESTRATOR_ROOM_IDS
    : [CORE_ROOM_ID, '__expert_vault__', '__expert_coding__'];
  if (orchIds.includes(roomId)) {
    ensureCoreRoom();
    return chatRooms.find(r => r && (r.id === roomId || r.sessionId === roomId)) || null;
  }
  return chatRooms.find(r => r && (r.id === roomId || r.sessionId === roomId)) || null;
}

function findChatRoom(query) {
  if (!query) return null;
  const raw = String(query).trim();
  if (!raw || raw.length < 2) return null;

  // 1. 접두사(#, room:, chat: 등) 정리
  const clean = raw.replace(/^(?:#|room:\/\/|chat:\/\/|room:|chat:)/i, '').trim();
  if (!clean) return null;
  const lower = clean.toLowerCase();

  // 2. 특수/고정 오케스트레이터 및 전문가 룸 별칭 매칭
  if (/^(?:마스터|마스터\s*에이전트|마스터\s*오케스트레이터|__core__)$/i.test(lower)) {
    ensureCoreRoom();
    return getRoomById(CORE_ROOM_ID);
  }
  if (/^(?:세컨브레인|세컨브레인\s*에이전트|세컨브레인\s*전문가|__expert_vault__)$/i.test(lower)) {
    ensureCoreRoom();
    return getRoomById(EXPERT_VAULT_ROOM_ID);
  }
  if (/^(?:봇개발|봇개발\s*에이전트|봇개발\s*전문가|__expert_coding__)$/i.test(lower)) {
    ensureCoreRoom();
    return getRoomById(EXPERT_CODING_ROOM_ID);
  }

  // 3. ID 또는 sessionId 정확 일치
  let matched = chatRooms.find(r => r && (r.id === clean || (r.sessionId && r.sessionId === clean)));
  if (matched) return matched;

  // 4. 제목 완전 일치 (공백/따옴표/특수문자 트림)
  const stripTitle = s => String(s || '')
    .trim()
    .replace(/^["'`\[\]]|["'`\[\]]$/g, '')
    .replace(/\s+/g, ' ')
    .trim()
    .toLowerCase();
  const cleanNorm = stripTitle(clean);
  if (!cleanNorm) return null;

  matched = chatRooms.find(r => r && r.title && stripTitle(r.title) === cleanNorm);
  if (matched) return matched;

  // 4-1. ' 세션', ' 작업', ' 채팅방', ' 대화방' 등의 접미사 제거 후 일치 시도
  const strippedSuffix = cleanNorm.replace(/\s*(?:세션|작업|채팅방|대화방|방)$/, '').trim();
  if (strippedSuffix && strippedSuffix.length >= 2 && strippedSuffix !== cleanNorm) {
    matched = chatRooms.find(r => r && r.title && stripTitle(r.title) === strippedSuffix);
    if (matched) return matched;
  }

  // 5. 제목 포함 관계 (엄격한 규칙 적용)
  // 중요 버그 방지: cleanNorm이 길다고 해서 짧은 방 제목(rNorm)을 포함한다고 매칭하면,
  // 흔한 짧은 단어(예: '스킨케어', '테스트')를 제목으로 가진 엉뚱한 대화방으로 잘못 이동하는 치명적 결함 발생!
  // 따라서 정방향(방 제목 rNorm이 검색어 cleanNorm을 포함)만 허용하되, 최소 4자 이상일 때만 매칭한다.
  if (cleanNorm.length >= 4) {
    matched = chatRooms.find(r => {
      if (!r || !r.title) return false;
      const rNorm = stripTitle(r.title);
      if (rNorm.length < 3) return false;
      if (rNorm.includes(cleanNorm)) return true;
      if (cleanNorm.includes(rNorm)) {
        // 역방향은 길이가 거의 같을 때(85% 이상 일치)만 엄격히 제한
        return (rNorm.length / cleanNorm.length) >= 0.85;
      }
      return false;
    });
    if (matched) return matched;
  }

  // 6. serverStatus.activeRuns 검사 (엄격한 기준 적용)
  // 중요 버그 방지: run.userText는 사용자의 긴 질문 본문이므로 이를 방 제목으로 간주해 매칭하면
  // 그 시점 다른 곳에서 돌고 있던 엉뚱한 세션으로 오연결된다!
  // 따라서 오직 run.roomId 일치 또는 명시적 run.title 일치만 허용한다.
  if (typeof serverStatus !== 'undefined' && serverStatus && Array.isArray(serverStatus.activeRuns)) {
    const active = serverStatus.activeRuns.find(run => {
      if (!run) return false;
      const rId = String(run.roomId || '').trim();
      if (rId === clean) return true;
      const rTitle = run.title ? stripTitle(run.title) : '';
      return rTitle && (rTitle === cleanNorm || (cleanNorm.length >= 4 && rTitle.includes(cleanNorm)));
    });
    if (active) {
      return {
        id: active.roomId,
        title: active.title || clean,
        isTemporary: true
      };
    }
  }

  return null;
}

function isSubtaskOrChildRoom(room, queryOrTitle = '') {
  const orchIds = (typeof ORCHESTRATOR_ROOM_IDS !== 'undefined' && Array.isArray(ORCHESTRATOR_ROOM_IDS))
    ? ORCHESTRATOR_ROOM_IDS
    : [CORE_ROOM_ID, '__expert_vault__', '__expert_coding__'];

  // 1. room 객체가 명시적으로 존재할 때
  if (room && room.id) {
    if (orchIds.includes(room.id)) return false;
    return true; // 최상위 3대 오케스트레이터가 아닌 모든 방은 서브세션/작업 세션
  }

  // 2. room 객체가 없더라도 문자열 식별자/제목으로 판별
  const q = String(queryOrTitle || '').trim();
  if (!q) return false;
  if (orchIds.includes(q)) return false;
  if (/^room-[a-zA-Z0-9_\-]+$/.test(q)) return true;
  if (/^(마스터|세컨브레인|봇개발|master|second_brain|bot_dev)$/i.test(q)) return false;

  // 대화 본문이나 칩에서 참조된 일반 제목/식별자는 기본적으로 서브세션으로 간주
  return true;
}

function navigateToChatRoom(roomIdOrTitle, preferThreadPanel = true) {
  const query = String(roomIdOrTitle || '').trim();
  if (!query) return;

  const room = findChatRoom(query);
  const targetId = (room && room.id) || (/^room-[a-zA-Z0-9_\-]+$/.test(query) ? query : null);
  const targetTitle = (room && room.title) || query;
  const isChild = isSubtaskOrChildRoom(room, targetId || query);

  // 하위 작업/서브세션 룸인 경우 화면 이동 대신 우측 스레드 패널에서 열람하도록 기본 동작 통일
  if (preferThreadPanel && isChild && typeof openThreadPanel === 'function') {
    openThreadPanel(null, targetId, targetTitle);
    return;
  }

  if (room && room.id) {
    selectRoom(room.id);
    showToast(`'${room.title || query}' 채팅방으로 이동했습니다.`);
    return;
  }

  // 만약 query 자체가 room-xxx 형식인 경우
  if (/^room-[a-zA-Z0-9_\-]+$/.test(query)) {
    if (preferThreadPanel && typeof openThreadPanel === 'function') {
      openThreadPanel(null, query, targetTitle);
      return;
    }
    selectRoom(query);
    showToast(`대화방(${query})으로 이동했습니다.`);
    return;
  }

  // 만약 아직 chatRooms 에 동기화되지 않은 신규 세션일 수 있으므로 서버 동기화 후 재시도
  if (typeof syncRoomsWithServer === 'function') {
    syncRoomsWithServer().then(() => {
      const reRoom = findChatRoom(query);
      const reTargetId = (reRoom && reRoom.id) || (/^room-[a-zA-Z0-9_\-]+$/.test(query) ? query : null);
      const reTargetTitle = (reRoom && reRoom.title) || query;
      const reIsChild = isSubtaskOrChildRoom(reRoom, reTargetId || query);

      if (preferThreadPanel && reIsChild && typeof openThreadPanel === 'function') {
        openThreadPanel(null, reTargetId, reTargetTitle);
        return;
      }
      if (reRoom && reRoom.id) {
        selectRoom(reRoom.id);
        showToast(`'${reRoom.title || query}' 채팅방으로 이동했습니다.`);
      } else if (/^room-[a-zA-Z0-9_\-]+$/.test(query)) {
        selectRoom(query);
        showToast(`대화방(${query})으로 이동했습니다.`);
      } else {
        showToast(`'${query}' 대화방을 찾을 수 없습니다.`);
      }
    }).catch(() => {
      if (preferThreadPanel && isSubtaskOrChildRoom(null, query) && typeof openThreadPanel === 'function') {
        openThreadPanel(null, /^room-[a-zA-Z0-9_\-]+$/.test(query) ? query : null, query);
        return;
      }
      if (/^room-[a-zA-Z0-9_\-]+$/.test(query)) {
        selectRoom(query);
      } else {
        showToast(`'${query}' 대화방을 찾을 수 없습니다.`);
      }
    });
  } else if (/^room-[a-zA-Z0-9_\-]+$/.test(query)) {
    selectRoom(query);
  } else {
    showToast(`'${query}' 대화방을 찾을 수 없습니다.`);
  }
}

function expandThreadToMainRoom() {
  if (typeof currentThreadRoomId !== 'undefined' && currentThreadRoomId) {
    const rid = currentThreadRoomId;
    if (typeof closeThreadPanel === 'function') closeThreadPanel();
    selectRoom(rid);
  }
}

function resolveChatRoomStatus(room, roomIdOrDisplay = '') {
  let roomId = room ? (room.id || '') : '';
  if (!roomId && typeof roomIdOrDisplay === 'string') {
    if (roomIdOrDisplay.startsWith('room-')) {
      roomId = roomIdOrDisplay;
    } else if (typeof findChatRoom === 'function') {
      const found = findChatRoom(roomIdOrDisplay);
      if (found && found.id) roomId = found.id;
    }
  }

  // 0. Fail-safe: 현재 대화방 타임라인 내에 해당 하위 작업의 바이패스 보고 메시지가 이미 도착해 있는지 확인
  // 바이패스 완료/오류 메시지가 존재한다는 것은 해당 하위 작업이 이미 끝났음을 나타내는 절대적 증거이다.
  const curRoom = typeof getCurrentRoom === 'function' ? getCurrentRoom() : null;
  if (curRoom && Array.isArray(curRoom.messages) && (roomId || roomIdOrDisplay)) {
    const checkTargetId = roomId;
    const checkTargetTitle = String(roomIdOrDisplay || (room && room.title) || '').trim().toLowerCase();
    const bypassMsg = curRoom.messages.find(m => {
      if (!m) return false;
      if (checkTargetId && m.bypassedFromRoomId === checkTargetId) return true;
      if (Array.isArray(m.steps)) {
        if (checkTargetId && m.steps.some(s => s && (s.roomId === checkTargetId || s.bypassedFromRoomId === checkTargetId))) return true;
        if (checkTargetTitle && checkTargetTitle.length >= 2 && m.steps.some(s => s && s.title && String(s.title).trim().toLowerCase() === checkTargetTitle)) return true;
      }
      if (checkTargetTitle && checkTargetTitle.length >= 2 && m.subagentTitle && String(m.subagentTitle).trim().toLowerCase() === checkTargetTitle) return true;
      return false;
    });
    if (bypassMsg && !bypassMsg.isSlotPlaceholder && String(bypassMsg.text || '').trim()) {
      if (checkTargetId && typeof roomLiveStates !== 'undefined' && roomLiveStates && roomLiveStates[checkTargetId]) {
        roomLiveStates[checkTargetId].isRunning = false;
        roomLiveStates[checkTargetId].feed = '';
      }
      const isErr = Boolean(bypassMsg.isError || (typeof bypassMsg.text === 'string' && (bypassMsg.text.startsWith('⚠️') || bypassMsg.text.startsWith('🛑'))));
      return isErr ? 'failed' : 'done';
    }
  }

  // 1. 실시간 실행 중 여부 확인
  if (roomId) {
    const live = (typeof roomLiveStates !== 'undefined' && roomLiveStates) ? roomLiveStates[roomId] : null;
    const hasServerActive = Boolean(typeof serverStatus !== 'undefined' && serverStatus && Array.isArray(serverStatus.activeRuns));
    const isActiveInServer = hasServerActive && serverStatus.activeRuns.some(r => r && (r.roomId === roomId || r.id === roomId));

    if (live && live.isRunning) {
      if (hasServerActive && !isActiveInServer) {
        // 서버의 활성 실행 목록(activeRuns)에 없는 고착된 클라이언트 상태 자동 치유
        live.isRunning = false;
        live.feed = '';
      } else {
        return 'running';
      }
    }
    if (isActiveInServer) return 'running';
  }

  // 2. masterTasks 확인 (하위 작업인 경우)
  if (typeof latestMasterTasks !== 'undefined' && Array.isArray(latestMasterTasks)) {
    const task = latestMasterTasks.find(t => t && (t.roomId === roomId || (roomId && t.id === roomId)));
    if (task) {
      const tStatus = String(task.status || '').toLowerCase();
      if (tStatus === 'running' || tStatus === 'in_progress' || tStatus === 'processing') return 'running';
      if (tStatus === 'failed' || tStatus === 'error' || tStatus === 'stalled' || tStatus === 'cancelled') return 'failed';
      if (tStatus === 'done' || tStatus === 'completed' || tStatus === 'success') return 'done';
    }
  }

  // 3. room 객체 자체 상태 확인
  if (room) {
    const rStatus = String(room.status || room.task_status || '').toLowerCase();
    if (rStatus === 'running') return 'running';
    if (rStatus === 'failed' || rStatus === 'error' || rStatus === 'cancelled') return 'failed';
    if (rStatus === 'done' || rStatus === 'completed' || rStatus === 'success') return 'done';

    // 만약 하위 세션/채팅(isSubtaskOrChildRoom)이라면: 실행 중이 아니면 기본적으로 완료
    if (typeof isSubtaskOrChildRoom === 'function' && isSubtaskOrChildRoom(room, roomId || roomIdOrDisplay)) {
      if (room.isFailed || room.hasError) return 'failed';
      return 'done';
    }
  } else if (roomId && /^room-[a-zA-Z0-9_\-]+$/.test(roomId)) {
    if (typeof getRoomById === 'function') {
      const targetRoom = getRoomById(roomId);
      if (targetRoom) return resolveChatRoomStatus(targetRoom, roomId);
    }
  }

  return '';
}

function updateInlineChatChipsStatus(targetRoomId, explicitStatus = null, targetTitle = '') {
  if (!targetRoomId && !targetTitle) return;
  const selectors = [];
  if (targetRoomId) selectors.push(`.md-link-chip[data-room-id="${targetRoomId}"]`);
  if (targetTitle) selectors.push(`.md-link-chip[data-title="${targetTitle}"]`);
  const chips = document.querySelectorAll(selectors.join(','));
  if (!chips || chips.length === 0) return;

  let nextStatus = explicitStatus;
  if (!nextStatus && targetRoomId) {
    const r = typeof getRoomById === 'function' ? getRoomById(targetRoomId) : null;
    nextStatus = resolveChatRoomStatus(r, targetRoomId);
  }

  chips.forEach(chip => {
    if (nextStatus) {
      chip.setAttribute('data-status', nextStatus);
    } else {
      chip.removeAttribute('data-status');
    }
    // title 속성 툴팁도 상태에 맞게 동기화
    const curTitle = chip.getAttribute('title') || '';
    if (curTitle) {
      let updatedTitle = curTitle.replace(/ · 상태: (?:실행 중|완료|실패)/g, '');
      if (nextStatus === 'running') updatedTitle = updatedTitle.replace(/(\s*\(클릭 시)/, ' · 상태: 실행 중$1');
      else if (nextStatus === 'done') updatedTitle = updatedTitle.replace(/(\s*\(클릭 시)/, ' · 상태: 완료$1');
      else if (nextStatus === 'failed') updatedTitle = updatedTitle.replace(/(\s*\(클릭 시)/, ' · 상태: 실패$1');
      chip.setAttribute('title', updatedTitle);
    }
  });
}

function updateAllInlineChatChips() {
  const chips = document.querySelectorAll('.md-link-chip[data-room-id], .md-link-chip[data-title]');
  if (!chips || chips.length === 0) return;
  chips.forEach(chip => {
    const rId = chip.getAttribute('data-room-id');
    const title = chip.getAttribute('data-title');
    const r = (rId && typeof getRoomById === 'function') ? getRoomById(rId) : null;
    const st = resolveChatRoomStatus(r, rId || title);
    if (st) {
      chip.setAttribute('data-status', st);
    } else {
      chip.removeAttribute('data-status');
    }
    const curTitle = chip.getAttribute('title') || '';
    if (curTitle) {
      let updatedTitle = curTitle.replace(/ · 상태: (?:실행 중|완료|실패)/g, '');
      if (st === 'running') updatedTitle = updatedTitle.replace(/(\s*\(클릭 시)/, ' · 상태: 실행 중$1');
      else if (st === 'done') updatedTitle = updatedTitle.replace(/(\s*\(클릭 시)/, ' · 상태: 완료$1');
      else if (st === 'failed') updatedTitle = updatedTitle.replace(/(\s*\(클릭 시)/, ' · 상태: 실패$1');
      chip.setAttribute('title', updatedTitle);
    }
  });
}

function buildChatRoomChipHtml(room, explicitLabel = '') {
  const roomId = room ? (room.id || '') : '';
  const roomTitle = room ? (room.title || '') : '';
  const display = String(explicitLabel || roomTitle || roomId || '채팅방').trim();
  const isChild = isSubtaskOrChildRoom(room, roomId || display);
  const status = resolveChatRoomStatus(room, roomId || display);

  let statusText = '';
  if (status === 'running') statusText = ' · 상태: 실행 중';
  else if (status === 'done') statusText = ' · 상태: 완료';
  else if (status === 'failed') statusText = ' · 상태: 실패';

  const titleAttr = roomTitle
    ? `${roomTitle}${statusText} (클릭 시 ${isChild ? '우측 사이드 패널에서 열람' : '해당 채팅방으로 이동'})`
    : `${display}${statusText} (클릭 시 ${isChild ? '우측 사이드 패널에서 열람' : '해당 채팅방으로 이동'})`;

  let iconSvg = ICONS.chat || '<svg class="i" viewBox="0 0 24 24"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>';
  let roleClass = 'is-room';
  let metaText = '';

  if (roomId === CORE_ROOM_ID) {
    iconSvg = ICONS.spark;
    roleClass += ' is-core';
    metaText = '마스터';
  } else if (roomId === EXPERT_VAULT_ROOM_ID) {
    iconSvg = ICONS.brain;
    roleClass += ' is-expert-vault';
    metaText = '세컨브레인';
  } else if (roomId === EXPERT_CODING_ROOM_ID) {
    iconSvg = ICONS.terminal;
    roleClass += ' is-expert-coding';
    metaText = '봇개발';
  } else if (display.length < 18) {
    metaText = '채팅';
  }

  const jsTarget = String(roomId || display).replace(/\\/g, '\\\\').replace(/'/g, "\\'");
  const jsTitle = String(roomTitle || display).replace(/\\/g, '\\\\').replace(/'/g, "\\'");
  const metaHtml = metaText
    ? `<span class="md-link-meta">· ${escapeHtml(metaText)}</span>`
    : '';

  const clickHandler = isChild
    ? `if(typeof openThreadPanel==='function'){openThreadPanel(null, '${escapeHtml(roomId)}', '${escapeHtml(jsTitle)}');}else{navigateToChatRoom('${escapeHtml(jsTarget)}');}`
    : `navigateToChatRoom('${escapeHtml(jsTarget)}')`;

  const statusHtml = `
    <span class="chip-status-wrap" aria-hidden="true">
      <span class="chip-status-icon is-running" title="실행 중"><span class="chip-spin-wheel"></span></span>
      <span class="chip-status-icon is-done" title="완료"><svg class="i chip-status-svg" viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12"/></svg></span>
      <span class="chip-status-icon is-failed" title="실패"><svg class="i chip-status-svg" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg></span>
    </span>
  `;

  const statusAttr = status ? ` data-status="${escapeHtml(status)}"` : '';
  const roomIdAttr = roomId ? ` data-room-id="${escapeHtml(roomId)}"` : '';
  const titleAttrProp = display ? ` data-title="${escapeHtml(display)}"` : '';

  return `<button type="button" class="md-link-chip ${roleClass}"${roomIdAttr}${titleAttrProp}${statusAttr} onclick="${clickHandler}" title="${escapeHtml(titleAttr)}">${iconSvg}<span class="md-link-label">${escapeHtml(display)}</span>${metaHtml}${statusHtml}<svg class="i ext-arrow" viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></button>`;
}

function ensureRoomLiveState(roomId) {
  if (!roomLiveStates[roomId]) {
    roomLiveStates[roomId] = { isRunning: false, jobId: '', feed: '', steps: [], streamText: '', startedAt: 0, queueCount: 0 };
  }
  return roomLiveStates[roomId];
}

function mergeToolSteps(localSteps, serverSteps) {
  const local = filterToolSteps(Array.isArray(localSteps) ? localSteps : []);
  const server = filterToolSteps(Array.isArray(serverSteps) ? serverSteps : []);
  if (local.length === 0) return [...server];
  if (server.length === 0) return [...local];

  const maxLen = Math.max(local.length, server.length);
  const merged = [];
  for (let i = 0; i < maxLen; i++) {
    const lStep = local[i];
    const sStep = server[i];
    if (lStep && sStep) {
      // 로컬이 이미 완료(done/error) 상태를 수신했다면 로컬 완료 상태 우선
      const isDone = (lStep.status === 'done' || lStep.status === 'error')
        ? lStep.status
        : ((sStep.status === 'done' || sStep.status === 'error') ? sStep.status : (lStep.status || 'running'));
      const sDetailLen = String(sStep.detail || '').length;
      const lDetailLen = String(lStep.detail || '').length;
      const detail = (lDetailLen >= sDetailLen)
        ? (lStep.detail || sStep.detail || '')
        : (sStep.detail || lStep.detail || '');
      merged.push({
        ...sStep,
        ...lStep, // 로컬 프로퍼티를 우선하여 실시간 수신된 도구명·타이틀·스텝인덱스 보존
        status: isDone,
        detail: detail,
        elapsedSeconds: lStep.elapsedSeconds || sStep.elapsedSeconds || '',
        title: lStep.title || sStep.title || '',
        tool: lStep.tool || sStep.tool || '',
        kind: lStep.kind || sStep.kind || 'tool_call'
      });
    } else if (lStep) {
      merged.push(lStep);
    } else if (sStep) {
      merged.push(sStep);
    }
  }
  return merged;
}

function applyActiveRunsSnapshot(activeRuns, isFullSnapshot = true) {
  if (!Array.isArray(activeRuns)) return;
  const activeRoomIds = new Set();
  let currentRoomAffected = false;

  for (const run of activeRuns) {
    if (!run || typeof run !== 'object') continue;
    const rawRoomId = String(run.roomId || run.sessionId || '').trim();
    if (!rawRoomId) continue;

    let room = getRoomById(rawRoomId) || (run.sessionId ? getRoomById(run.sessionId) : null);
    if (!room) {
      room = {
        id: rawRoomId,
        title: rawRoomId === CORE_ROOM_ID ? CORE_ROOM_TITLE : ((run.userText || '새 대화').slice(0, 24)),
        preview: run.userText || '',
        messageCount: 1,
        messages: [],
        isNewSession: !run.sessionId,
        sessionId: run.sessionId || null,
        updatedAt: Date.now()
      };
      chatRooms.unshift(room);
    }
    activeRoomIds.add(room.id);
    if (run.sessionId) activeRoomIds.add(String(run.sessionId));
    activeRoomIds.add(rawRoomId);

    if (!Array.isArray(room.messages)) room.messages = [];

    const activeUserText = String(run.userText || '').trim();
    if (activeUserText) {
      const incomingRole = run.channel === 'task_report' ? 'sub-agent' : 'user';
      upsertRoomMessage(room, {
        id: run.userMsgId || undefined,
        clientMsgId: run.clientMsgId || undefined,
        sender: incomingRole,
        role: incomingRole,
        text: activeUserText,
        channel: run.channel || 'web',
        createdAt: run.startedAt || Date.now()
      }, { clearQueued: true, beforeQueued: true });
    }

    if (Array.isArray(run.queuedMessages)) {
      const nowMs = Date.now();
      const serverQueuedClientIds = new Set(
        run.queuedMessages.map(qm => qm && qm.clientMsgId).filter(Boolean)
      );
      const serverQueuedTexts = new Set(
        run.queuedMessages.map(qm => qm && qm.text ? String(qm.text).trim() : '').filter(Boolean)
      );
      room.messages = room.messages.filter(m => {
        if (!m || !m.queued) return true;
        if (m._localPendingAt && (nowMs - m._localPendingAt) < 3000) return true;
        if (m.clientMsgId && serverQueuedClientIds.has(m.clientMsgId)) return true;
        if (!m.clientMsgId && m.text && serverQueuedTexts.has(String(m.text).trim())) return true;
        return false;
      });
      run.queuedMessages.forEach((qm, idx) => {
        if (qm && qm.text) {
          const qRole = qm.channel === 'task_report' ? 'sub-agent' : 'user';
          upsertRoomMessage(room, {
            id: qm.userMsgId || undefined,
            clientMsgId: qm.clientMsgId || undefined,
            sender: qRole,
            role: qRole,
            text: qm.text,
            channel: qm.channel || 'web',
            queued: true,
            queuePos: qm.queuePos || (idx + 1),
            createdAt: Date.now()
          });
        }
      });
    }

    const liveState = ensureRoomLiveState(room.id);
    const sameJob = Boolean(
      (!liveState.jobId || !run.jobId || liveState.jobId === run.jobId)
    );
    if (run.jobId && !liveState.jobId) {
      liveState.jobId = run.jobId;
    }
    if (run.asstMsgId && !liveState.asstMsgId) {
      liveState.asstMsgId = run.asstMsgId;
    }
    liveState.isRunning = Boolean(run.isRunning !== false);
    liveState.startedAt = run.startedAt || liveState.startedAt || Date.now();
    if (run.model) liveState.model = run.model;
    if (run.effort) liveState.effort = run.effort;

    const serverSteps = filterToolSteps(run.steps || []);
    // 핵심: local에 이미 스텝(activeSteps)이 누적되어 있다면 jobId 일치 여부와 무관하게 절대 통째로 덮어쓰거나 날리지 않고 항상 안전 병합 보존!
    if (Array.isArray(liveState.steps) && liveState.steps.length > 0) {
      liveState.steps = mergeToolSteps(liveState.steps, serverSteps);
    } else if (serverSteps.length > 0) {
      liveState.steps = serverSteps;
    }

    const serverStream = String(run.streamText || '');
    const localStream = String(liveState.streamText || '');
    // 로컬에 이미 더 긴 스트리밍 텍스트가 있다면 서버의 축소/지연 스냅샷으로 덮어쓰지 않고 로컬 보존
    if (serverStream.length >= localStream.length && serverStream.length > 0) {
      liveState.streamText = serverStream;
    } else if (!liveState.streamText && serverStream.length > 0) {
      liveState.streamText = serverStream;
    }

    if (run.feed && (!liveState.feed || liveState.feed === '생각하는 중…' || run.feed !== '생각하는 중…')) {
      liveState.feed = run.feed;
    } else if (!liveState.feed) {
      liveState.feed = liveState.streamText ? '답변 작성 중…' : '생각하는 중…';
    }
    liveState.queueCount = typeof run.queueCount === 'number'
      ? run.queueCount
      : renumberQueuedMessages(room);

    if (room.id === currentRoomId) {
      currentRoomAffected = true;
    }
  }

  if (isFullSnapshot) {
    for (const [rId, st] of Object.entries(roomLiveStates)) {
      if (!st || !st.isRunning) continue;
      const roomObj = getRoomById(rId);
      const isKnownActive = activeRoomIds.has(rId) || (
        roomObj && (
          activeRoomIds.has(roomObj.id) ||
          (roomObj.sessionId && activeRoomIds.has(String(roomObj.sessionId)))
        )
      );
      if (isKnownActive) continue;

      // 30분(1800초) 이상 경과한 세션은 좀비로 간주하고 무조건 회수
      const elapsedMs = st.startedAt ? (Date.now() - st.startedAt) : 0;
      if (elapsedMs >= 1800000) {
        st.isRunning = false;
        const finishedRoom = getRoomById(rId);
        if (finishedRoom) {
          loadChatHistoryFromServer(finishedRoom.id, finishedRoom.sessionId);
        }
        if (rId === currentRoomId) {
          currentRoomAffected = true;
        }
        continue;
      }

      // 방금 전송된 직후(15초 이내)이거나 진행 중인 단계가 있는 경우, 웹소켓 연결 중에는 일시적 유예
      const isCurrentActive = (rId === currentRoomId);
      const hasLiveContent = (Array.isArray(st.steps) && st.steps.length > 0) || Boolean(st.streamText);
      const isWsActive = socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING);
      const isRecent = elapsedMs < 15000;
      if ((isCurrentActive || hasLiveContent) && isWsActive && isRecent) {
        continue;
      }

      st.isRunning = false;
      st.jobId = '';
      st.asstMsgId = null;
      st.feed = '';
      st.steps = [];
      st.streamText = '';
      st.startedAt = 0;
      st.queueCount = 0;
      const finishedRoom = getRoomById(rId);
      if (finishedRoom) {
        loadChatHistoryFromServer(finishedRoom.id, finishedRoom.sessionId);
      }
      if (typeof updateSubAgentChipsStatus === 'function') {
        updateSubAgentChipsStatus(rId, 'done', '정상 완료', finishedRoom ? finishedRoom.title : '');
      }
      if (rId === currentRoomId) {
        currentRoomAffected = true;
      }
    }

    // 활성 실행 목록(activeRuns)에 없는 화면 내 모든 실행 중 칩을 완료 상태로 리컨사일
    if (typeof updateSubAgentChipsStatus === 'function') {
      document.querySelectorAll('.sub-agent-chip.is-running').forEach(chip => {
        const rid = chip.getAttribute('data-room-id');
        if (rid && !activeRoomIds.has(rid) && (!roomLiveStates[rid] || !roomLiveStates[rid].isRunning)) {
          updateSubAgentChipsStatus(rid, 'done', '정상 완료');
        }
      });
    }
  }

  syncLiveElapsedTimer();
  renderRoomList();

  const curRoom = getCurrentRoom();
  const curLive = curRoom ? roomLiveStates[curRoom.id] : null;
  if (curLive && curLive.isRunning) {
    setAgentAvatarStatus('working', curLive.feed || '실행 중…');
    setOrbState('thinking', curLive.feed || '생각하는 중…');
  } else if (!Object.values(roomLiveStates).some(s => s && s.isRunning)) {
    setAgentAvatarStatus('idle', '대기 중');
    if (!isVoiceRecording) setOrbState('idle', '준비 완료');
  }

  if (currentRoomAffected || (curLive && curLive.isRunning)) {
    renderMessagesForCurrentRoom(true);
    updateComposerStatusBar();
    updateSendButtonState();
  }
  if (typeof updateAllInlineChatChips === 'function') {
    updateAllInlineChatChips();
  }
}

let _liveTimerInterval = null;
function syncLiveElapsedTimer() {
  const anyRunning = Object.values(roomLiveStates).some(st => st && st.isRunning);
  if (anyRunning && !_liveTimerInterval) {
    _liveTimerInterval = setInterval(() => {
      const curRoom = getCurrentRoom();
      if (!curRoom) return;
      const st = roomLiveStates[curRoom.id];
      const el = document.getElementById('live-elapsed-timer');
      if (st && st.isRunning && st.startedAt && el) {
        const sec = Math.max(0, Math.floor((Date.now() - st.startedAt) / 1000));
        el.textContent = sec >= 1 ? `${sec}s` : '';
      }
    }, 500);
  } else if (!anyRunning && _liveTimerInterval) {
    clearInterval(_liveTimerInterval);
    _liveTimerInterval = null;
  }
}

function updateLiveFeedDom(feedText) {
  const feedEl = document.getElementById('live-feed-text');
  if (!feedEl) {
    renderMessagesForCurrentRoom();
    return;
  }
  if (feedText) {
    feedEl.textContent = feedText;
  }
}

let _roomsRefreshTimer = null;
function scheduleRoomsRefreshFromEvent() {
  if (_roomsRefreshTimer) return;
  _roomsRefreshTimer = setTimeout(() => {
    _roomsRefreshTimer = null;
    loadChatRoomsFromServer(false, true);
  }, 250);
}

function resolveEventRoom(data) {
  if (data && data.roomId) {
    return getRoomById(data.roomId) || (data.sessionId ? getRoomById(data.sessionId) : null) || null;
  }
  const fallbackId = lastRequestedRoomId || currentRoomId;
  return getRoomById(fallbackId) || getCurrentRoom();
}

function handleWsEvent(data) {
  if (data.type === 'orchestrator_skill_done') {
    if (data.skill === 'memory-rebalance') {
      const btn = document.getElementById('btn-msb-mem-rebalance');
      if (btn) {
        btn.disabled = false;
        btn.innerHTML = `<svg class="i" viewBox="0 0 24 24" style="width:13px;height:13px;"><path d="m16 16 3-8 3 8c-.87.65-1.92 1-3 1s-2.13-.35-3-1Z"/><path d="m2 16 3-8 3 8c-.87.65-1.92 1-3 1s-2.13-.35-3-1Z"/><path d="M7 21h10"/><path d="M12 3v18"/><path d="M3 7h2c2 0 5-1 7-2 2 1 5 2 7 2h2"/></svg><span>리밸런싱</span>`;
      }
      if (data.ok && data.memory) {
        latestMasterMemory = data.memory;
        if (latestMasterGraph) latestMasterGraph.memory = data.memory;
        renderMsbMemory(data.memory);
        showToast('메모리 리밸런싱 완료');
      } else if (!data.ok) {
        showToast('리밸런싱 실패: ' + (data.error || '알 수 없는 오류'));
      }
    }
    return;
  }
  if (data.type === 'automation_updated' || data.type === 'scheduler_notice') {
    if (data.type === 'scheduler_notice' && data.text && data.roomId !== currentRoomId) {
      showToast(data.text, { roomId: data.roomId || CORE_ROOM_ID });
    }
    if (currentView === 'automation' && typeof loadAutomationData === 'function') {
      loadAutomationData();
    }
    return;
  }
  if (data.type === 'master_graph') {
    if (data.graph) {
      handleMasterGraphWsEvent(data.graph);
    }
    if (typeof updateAllInlineChatChips === 'function') {
      updateAllInlineChatChips();
    }
    return;
  }
  if (data.type === 'agent_configs_updated') {
    if (Array.isArray(data.agents) && typeof syncAgentConfigsState === 'function') {
      syncAgentConfigsState(data.agents, data.orchestratorRoomIds);
    }
    return;
  }
  if (data.type === 'prompt_layers_updated') {
    const modal = document.getElementById('modal-prompts-center');
    if (modal && modal.classList.contains('open') && typeof window.selectPromptLayer === 'function') {
      window.selectPromptLayer(data.targetId || 'common', true);
    }
    return;
  }
  if (data.type === 'session_reset') {
    if (data.roomId && (data.roomId === CORE_ROOM_ID || ORCHESTRATOR_ROOM_IDS.includes(data.roomId))) {
      applyOrchestratorSessionResetLocal(data.roomId, data);
    }
    return;
  }
  if (data.type === 'status') {
    applyStatusPayload(data);
    return;
  }
  if (data.type === 'session_deleted') {
    const delSet = new Set();
    if (data.roomId) delSet.add(String(data.roomId).trim());
    if (data.sessionId) delSet.add(String(data.sessionId).trim());
    if (Array.isArray(data.roomIds)) data.roomIds.forEach(x => { if (x) delSet.add(String(x).trim()); });
    if (Array.isArray(data.sessionIds)) data.sessionIds.forEach(x => { if (x) delSet.add(String(x).trim()); });
    delSet.delete('');
    if (delSet.size > 0) {
      for (const k of delSet) {
        delete roomLiveStates[k];
        selectedRoomIds.delete(k);
      }
      const wasCur = delSet.has(String(currentRoomId));
      const beforeLen = chatRooms.length;
      chatRooms = chatRooms.filter(r => {
        if (!r) return false;
        const rid = String(r.id || '').trim();
        const sid = String(r.sessionId || '').trim();
        return !delSet.has(rid) && (!sid || !delSet.has(sid));
      });
      if (chatRooms.length !== beforeLen || wasCur) {
        saveChatRoomsToStorage(true);
        if (wasCur) {
          if (chatRooms.length > 0) {
            selectRoom(chatRooms[0].id);
          } else {
            createNewChat(true);
          }
        } else {
          renderRoomList();
        }
      }
    }
    return;
  }
  if (data.type === 'room_settings_updated') {
    const targetId = String(data.roomId || '').trim();
    const room = getRoomById(targetId);
    if (room) {
      if (data.model !== undefined) room.model = data.model || null;
      if (data.effort !== undefined) room.effort = data.effort || null;
      if (data.workdir !== undefined) room.workdir = data.workdir || null;
      if (data.defaultModel !== undefined) room.defaultModel = data.defaultModel;
      if (data.defaultEffort !== undefined) room.defaultEffort = data.defaultEffort;
      saveChatRoomsToStorage();
      if (targetId === currentRoomId) {
        updateRoomConfigBar();
      }
    }
    return;
  }
  if (data.type === 'room_renamed') {
    const targetId = String(data.roomId || data.sessionId || '').trim();
    const newTitle = String(data.title || '').trim();
    if (targetId && newTitle) {
      const room = getRoomById(targetId) || (data.sessionId ? getRoomById(String(data.sessionId).trim()) : null);
      if (room) {
        room.title = newTitle;
        renderRoomList();
        updateTopbarTitle();
      }
    }
    return;
  }
  if (data.type === 'room_created' && data.room) {
    const sRoom = data.room;
    const rId = String(sRoom.id || sRoom.roomId || '').trim();
    const isChild = Boolean(data.byOrchestrator || sRoom.byOrchestrator || sRoom.isOrchestratorChild || sRoom.channel === 'orchestrator');
    const resolvedParent = sRoom.parentAgentId || data.parentAgentId || (isChild ? CORE_ROOM_ID : null);
    let existing = getRoomById(rId);
    if (!existing && rId) {
      existing = {
        id: rId,
        roomId: rId,
        title: sRoom.title || '새 에이전트 세션',
        preview: sRoom.preview || '',
        messageCount: sRoom.messageCount || 0,
        messages: [],
        isNewSession: true,
        sessionId: sRoom.sessionId || null,
        byOrchestrator: isChild,
        isOrchestratorChild: isChild,
        parentAgentId: resolvedParent,
        historyLoaded: false,
        updatedAt: Date.now(),
        createdAt: Date.now(),
        model: sRoom.model || null,
        effort: sRoom.effort || null
      };
      chatRooms.unshift(existing);
    } else if (existing) {
      if (sRoom.title) existing.title = sRoom.title;
      if (sRoom.preview) existing.preview = sRoom.preview;
      if (sRoom.model) existing.model = sRoom.model;
      if (sRoom.effort) existing.effort = sRoom.effort;
      if (resolvedParent) existing.parentAgentId = resolvedParent;
      if (isChild) {
        existing.byOrchestrator = true;
        existing.isOrchestratorChild = true;
      }
      existing.updatedAt = Date.now();
    }
    sortChatRooms();
    saveChatRoomsToStorage();
    renderRoomList();
    if (data.byOrchestrator) {
      let creatorLabel = '마스터 에이전트';
      if (resolvedParent === EXPERT_VAULT_ROOM_ID) creatorLabel = '세컨브레인 전문가';
      else if (resolvedParent === EXPERT_CODING_ROOM_ID) creatorLabel = '봇개발 전문가';
      showToast(`${creatorLabel}가 새 세션 '${sRoom.title || rId}'을 생성했습니다.`, { roomId: sRoom.id || rId });
    }
    return;
  }
  if (data.type === 'orchestrator_dispatch') {
    const targetId = String(data.roomId || data.sessionId || '').trim();
    const dispatchParent = data.parentAgentId || CORE_ROOM_ID;
    let targetRoom = getRoomById(targetId);
    if (!targetRoom && targetId) {
      targetRoom = {
        id: targetId,
        roomId: targetId,
        title: data.title || '에이전트 세션',
        preview: data.text || '',
        messageCount: 1,
        messages: [],
        isNewSession: !data.sessionId,
        sessionId: data.sessionId || null,
        byOrchestrator: true,
        isOrchestratorChild: true,
        parentAgentId: dispatchParent,
        historyLoaded: false,
        updatedAt: Date.now(),
        createdAt: Date.now(),
        model: data.model || null,
        effort: data.effort || null
      };
      chatRooms.unshift(targetRoom);
    } else if (targetRoom) {
      if (data.model !== undefined && data.model !== null) targetRoom.model = data.model;
      if (data.effort !== undefined && data.effort !== null) targetRoom.effort = data.effort;
      if (dispatchParent) targetRoom.parentAgentId = dispatchParent;
      targetRoom.isOrchestratorChild = true;
    }
    if (targetRoom && data.text) {
      const liveState = ensureRoomLiveState(targetRoom.id);
      const isQueuedDispatch = Boolean(data.queued || liveState.isRunning || getQueuedMessagesCount(targetRoom) > 0);
      upsertRoomMessage(targetRoom, {
        id: data.msgId || undefined,
        clientMsgId: data.clientMsgId || undefined,
        sender: 'user',
        role: 'user',
        text: data.text,
        channel: 'orchestrator',
        queued: isQueuedDispatch,
        createdAt: Date.now()
      }, { beforeQueued: !isQueuedDispatch });
      liveState.queueCount = renumberQueuedMessages(targetRoom);
      targetRoom.preview = String(data.text).slice(0, 60);
      targetRoom.messageCount = targetRoom.messages.length;
      targetRoom.updatedAt = Date.now();
      targetRoom.byOrchestrator = true;
      targetRoom.isOrchestratorChild = true;
      sortChatRooms();
      saveChatRoomsToStorage();
      renderRoomList();
      if (targetRoom.id === currentRoomId) {
        renderMessagesForCurrentRoom(true);
        updateRoomConfigBar();
        updateComposerStatusBar();
        updateSendButtonState();
      }
    }

    // 발주처(마스터/전문가) 방 타임라인 하단에 사전 예약 슬롯(slotMsgId)을 즉시 마운트 (패턴 E: 발주 순서 고정 및 기존 방 재사용 시 묻힘 방지)
    const uiRoomId = String(data.targetUiRoomId || data.originRoomId || dispatchParent || '').trim();
    const slotMsgId = (typeof data.slotMsgId === 'number' && data.slotMsgId > 0) ? data.slotMsgId : null;
    if (!data.wait && uiRoomId && slotMsgId) {
      const uiRoom = getRoomById(uiRoomId);
      if (uiRoom) {
        const slotStatus = data.slotStatus || (data.queued ? 'queued' : 'running');
        const slotSteps = Array.isArray(data.slotSteps) && data.slotSteps.length > 0
          ? data.slotSteps
          : [{
              kind: 'subagent_bypass',
              tool: 'bypass',
              isSlotPlaceholder: true,
              title: data.title || targetId,
              roomId: targetId,
              taskId: data.taskId || '',
              status: slotStatus,
              reason: slotStatus === 'queued' ? '대기열 대기 중…' : '백그라운드 실행 중…',
              reused: Boolean(data.reused),
              bypassedFromRoomId: targetId
            }];
        upsertRoomMessage(uiRoom, {
          id: slotMsgId,
          role: 'assistant',
          sender: 'assistant',
          channel: 'thread_bypass',
          text: '',
          steps: slotSteps,
          isSlotPlaceholder: true,
          slotTaskId: data.taskId || '',
          bypassedFromRoomId: targetId,
          subagentTitle: data.title || targetId,
          model: data.model || null,
          effort: data.effort || null,
          createdAt: Date.now()
        }, { beforeQueued: true });
        uiRoom.messageCount = uiRoom.messages.length;
        uiRoom.updatedAt = Date.now();
        saveChatRoomsToStorage();
        if (uiRoom.id === currentRoomId) {
          renderMessagesForCurrentRoom();
        }
      }
    }
    return;
  }
  const targetRoom = resolveEventRoom(data);
  if (!targetRoom) {
    if (data.roomId && (data.type === 'started' || data.type === 'queued' || data.type === 'result')) {
      scheduleRoomsRefreshFromEvent();
      if (typeof updateInlineChatChipsStatus === 'function') {
        const fallbackStatus = data.type === 'result' ? (data.interrupted ? 'failed' : 'done') : (data.type === 'started' ? 'running' : null);
        updateInlineChatChipsStatus(data.roomId, fallbackStatus);
        if (data.bypassedFromRoomId) {
          if (data.type === 'result' && typeof roomLiveStates !== 'undefined' && roomLiveStates && roomLiveStates[data.bypassedFromRoomId]) {
            roomLiveStates[data.bypassedFromRoomId].isRunning = false;
            roomLiveStates[data.bypassedFromRoomId].feed = '';
          }
          updateInlineChatChipsStatus(data.bypassedFromRoomId, data.isSuccess === false ? 'failed' : 'done', data.subagentTitle || '');
        }
        if (Array.isArray(data.delegatedRooms)) {
          data.delegatedRooms.forEach(item => {
            if (item && item.roomId) {
              const st = (item.status === 'error' || item.status === 'failed') ? 'failed' : (item.status || 'done');
              if (st !== 'running' && typeof roomLiveStates !== 'undefined' && roomLiveStates && roomLiveStates[item.roomId]) {
                roomLiveStates[item.roomId].isRunning = false;
                roomLiveStates[item.roomId].feed = '';
              }
              updateInlineChatChipsStatus(item.roomId, st, item.title || '');
            }
          });
        }
        if (typeof updateAllInlineChatChips === 'function') {
          updateAllInlineChatChips();
        }
      }
    }
    return;
  }
  const isCurrentRoom = targetRoom.id === currentRoomId;
  const isThreadRoom = Boolean(typeof currentThreadRoomId !== 'undefined' && currentThreadRoomId && (targetRoom.id === currentThreadRoomId || targetRoom.roomId === currentThreadRoomId));
  const liveState = ensureRoomLiveState(targetRoom.id);

  if (data.type === 'started') {
    if (Array.isArray(targetRoom.messages)) {
      const incomingRole = data.channel === 'task_report' ? 'sub-agent' : 'user';
      if (data.userText) {
        upsertRoomMessage(targetRoom, {
          id: data.userMsgId || undefined,
          clientMsgId: data.clientMsgId || undefined,
          sender: incomingRole,
          role: incomingRole,
          text: data.userText,
          channel: data.channel || 'web',
          createdAt: Date.now()
        }, { clearQueued: true, beforeQueued: true });
      } else {
        let qIdx = data.clientMsgId
          ? targetRoom.messages.findIndex(m => m && m.clientMsgId === data.clientMsgId)
          : -1;
        if (qIdx === -1) qIdx = targetRoom.messages.findIndex(m => m && m.queued);
        if (qIdx !== -1) {
          if (data.userMsgId) targetRoom.messages[qIdx].id = data.userMsgId;
          delete targetRoom.messages[qIdx].queued;
          delete targetRoom.messages[qIdx].queuePos;
          delete targetRoom.messages[qIdx]._localPendingAt;
        }
      }
      if (data.channel === 'orchestrator') {
        targetRoom.isOrchestratorChild = true;
      }
      targetRoom.messageCount = Math.max(targetRoom.messageCount || 0, targetRoom.messages.length);
      liveState.queueCount = renumberQueuedMessages(targetRoom);
      saveChatRoomsToStorage();
    }
    liveState.isRunning = true;
    if (typeof updateInlineChatChipsStatus === 'function') {
      updateInlineChatChipsStatus(targetRoom.id, 'running');
    }
    if (data.jobId) liveState.jobId = data.jobId;
    if (data.asstMsgId) liveState.asstMsgId = data.asstMsgId;
    liveState.feed = '생각하는 중…';
    liveState.steps = [];
    liveState.streamText = '';
    liveState.startedAt = data.startedAt || Date.now();
    if (data.model) liveState.model = data.model;
    if (data.effort) liveState.effort = data.effort;
    syncLiveElapsedTimer();
    setAgentAvatarStatus('thinking', '생각하는 중…');
    renderRoomList();
    if (isThreadRoom && typeof renderThreadPanelBody === 'function') {
      renderThreadPanelBody();
    }
    if (isCurrentRoom) {
      setOrbState('thinking', liveState.feed);
      renderMessagesForCurrentRoom();
      updateSendButtonState();
    } else if (targetRoom.isOrchestratorChild || data.channel === 'orchestrator' || data.channel === 'task_report' || targetRoom.byOrchestrator) {
      showToast(`[${targetRoom.title}] 작업이 시작되었습니다.`, { roomId: targetRoom.id });
    }
    if (typeof updateSubAgentChipsStatus === 'function') {
      updateSubAgentChipsStatus(targetRoom.id, 'running', liveState.feed || '생각하는 중…', targetRoom.title);
    }
  } else if (data.type === 'queued') {
    const qPos = data.position || data.queueSize || 1;
    liveState.isRunning = true;
    if (!liveState.startedAt) liveState.startedAt = Date.now();
    syncLiveElapsedTimer();
    if (typeof updateSubAgentChipsStatus === 'function') {
      updateSubAgentChipsStatus(targetRoom.id, 'running', '대기열 등록됨', targetRoom.title);
    }
    if (Array.isArray(targetRoom.messages)) {
      const qRole = data.channel === 'task_report' ? 'sub-agent' : 'user';
      if (data.text || data.clientMsgId || data.userMsgId) {
        upsertRoomMessage(targetRoom, {
          id: data.userMsgId || undefined,
          clientMsgId: data.clientMsgId || undefined,
          sender: qRole,
          role: qRole,
          text: data.text || '',
          channel: data.channel || 'web',
          queued: true,
          queuePos: qPos,
          createdAt: Date.now()
        });
      } else {
        for (let i = targetRoom.messages.length - 1; i >= 0; i--) {
          const m = targetRoom.messages[i];
          if (m && (m.sender === 'user' || m.role === 'user' || m.sender === 'sub-agent' || m.role === 'sub-agent')) {
            m.queued = true;
            m.queuePos = qPos;
            break;
          }
        }
      }
      liveState.queueCount = renumberQueuedMessages(targetRoom);
      saveChatRoomsToStorage();
    }
    showToast(`대기열에 추가되었습니다 (${qPos}번째)`);
    renderRoomList();
    if (isCurrentRoom) {
      if (!liveState.feed) liveState.feed = `답변 생성 중 (대기열 ${qPos}건)`;
      setOrbState('thinking', liveState.feed);
      renderMessagesForCurrentRoom(true);
      updateSendButtonState();
    }
  } else if (data.type === 'queued_cancelled') {
    if (Array.isArray(data.cancelledClientMsgIds) && data.cancelledClientMsgIds.length > 0) {
      const cancelSet = new Set(data.cancelledClientMsgIds);
      if (Array.isArray(targetRoom.messages)) {
        targetRoom.messages = targetRoom.messages.filter(m => !(m && m.clientMsgId && cancelSet.has(m.clientMsgId)));
      }
    }
    liveState.queueCount = renumberQueuedMessages(targetRoom);
    renderRoomList();
    if (isCurrentRoom) {
      renderMessagesForCurrentRoom();
      updateComposerStatusBar();
      updateSendButtonState();
    }
    if (typeof updateSubAgentChipsStatus === 'function') {
      const remainingQ = (typeof data.queueSize === 'number') ? data.queueSize : (liveState.queueCount || 0);
      if (remainingQ === 0 && !liveState.isRunning) {
        updateSubAgentChipsStatus(targetRoom.id, 'done', '대기열 취소됨', targetRoom.title);
      }
    }
  } else if (data.type === 'agent_step') {
    const stepObj = data.step || data;
    liveState.isRunning = true;
    if (!liveState.startedAt) liveState.startedAt = Date.now();
    syncLiveElapsedTimer();
    liveState.steps.push(stepObj);
    const isThinking = (stepObj.kind === 'thinking');
    liveState.feed = stepObj.title || (isThinking ? '생각하는 중…' : (stepObj.tool || '도구 실행 중…'));
    setAgentAvatarStatus(isThinking ? 'thinking' : 'working', liveState.feed);
    if (typeof updateSubAgentChipsStatus === 'function') {
      updateSubAgentChipsStatus(targetRoom.id, 'running', liveState.feed, targetRoom.title);
    }
    if (isCurrentRoom) {
      setOrbState('thinking', liveState.feed);
      appendLiveAgentStepToDom(stepObj);
      updateLiveFeedDom(liveState.feed);
    }
    if (isThreadRoom && typeof renderThreadPanelBody === 'function') {
      renderThreadPanelBody();
    }
  } else if (data.type === 'agent_step_update') {
    const stepObj = data.step || {};
    const sIdx = typeof data.stepIndex === 'number' ? data.stepIndex : (liveState.steps.length - 1);
    if (sIdx >= 0 && sIdx < liveState.steps.length) {
      liveState.steps[sIdx] = stepObj;
    }
    if (!liveState.streamText) {
      liveState.feed = '내용 분석 및 답변 정리 중…';
    }
    setAgentAvatarStatus('working', liveState.feed);
    if (typeof updateSubAgentChipsStatus === 'function') {
      updateSubAgentChipsStatus(targetRoom.id, 'running', liveState.feed, targetRoom.title);
    }
    if (isCurrentRoom) {
      setOrbState('thinking', liveState.feed);
      updateLiveAgentStepInDom(sIdx, stepObj, liveState.steps);
      updateLiveFeedDom(liveState.feed);
    }
    if (isThreadRoom && typeof renderThreadPanelBody === 'function') {
      renderThreadPanelBody();
    }
  } else if (data.type === 'stream_delta') {
    const delta = data.delta || '';
    if (!delta) return;
    liveState.isRunning = true;
    liveState.streamText = (liveState.streamText || '') + delta;
    liveState.feed = '답변 작성 중…';
    setAgentAvatarStatus('working', liveState.feed);
    if (typeof updateSubAgentChipsStatus === 'function') {
      updateSubAgentChipsStatus(targetRoom.id, 'running', liveState.feed, targetRoom.title);
    }
    if (isCurrentRoom) {
      setOrbState('thinking', liveState.feed);
      updateLiveStreamMarkdownDom(liveState.streamText);
      updateLiveFeedDom(liveState.feed);
    }
    if (isThreadRoom && typeof renderThreadPanelBody === 'function') {
      renderThreadPanelBody();
    }
  } else if (data.type === 'stream_chunk') {
    const chunkText = data.text || '';
    if (!chunkText) return;
    liveState.isRunning = true;
    liveState.streamText = chunkText;
    liveState.feed = '답변 작성 중…';
    setAgentAvatarStatus('working', liveState.feed);
    if (typeof updateSubAgentChipsStatus === 'function') {
      updateSubAgentChipsStatus(targetRoom.id, 'running', liveState.feed, targetRoom.title);
    }
    if (isCurrentRoom) {
      setOrbState('thinking', liveState.feed);
      updateLiveStreamMarkdownDom(liveState.streamText);
      updateLiveFeedDom(liveState.feed);
    }
    if (isThreadRoom && typeof renderThreadPanelBody === 'function') {
      renderThreadPanelBody();
    }
  } else if (data.type === 'progress') {
    liveState.isRunning = true;
    const nextFeed = data.feed || data.text || '';
    if (nextFeed && !liveState.streamText) {
      liveState.feed = nextFeed;
    }
    setAgentAvatarStatus('working', liveState.feed || '실행 중…');
    if (typeof updateSubAgentChipsStatus === 'function') {
      updateSubAgentChipsStatus(targetRoom.id, 'running', liveState.feed || '실행 중…', targetRoom.title);
    }
    if (isCurrentRoom) {
      setOrbState('thinking', liveState.feed || '실행 중…');
      updateLiveFeedDom(liveState.feed);
    }
    if (isThreadRoom && typeof renderThreadPanelBody === 'function') {
      renderThreadPanelBody();
    }
  } else if (data.type === 'cancelled') {
    if (typeof flushLiveStreamBuffer === 'function') {
      flushLiveStreamBuffer();
    }
    liveState.isRunning = false;
    if (typeof updateInlineChatChipsStatus === 'function') {
      updateInlineChatChipsStatus(targetRoom.id, 'failed');
    }
    liveState.feed = '';
    liveState.steps = [];
    liveState.streamText = '';
    liveState.startedAt = 0;
    liveState.queueCount = 0;
    syncLiveElapsedTimer();
    if (Array.isArray(targetRoom.messages)) {
      targetRoom.messages = targetRoom.messages.filter(m => !(m && m.queued));
      saveChatRoomsToStorage();
    }
    setAgentAvatarStatus('idle', '대기 중');
    if (data.sessionTerminated && targetRoom) targetRoom.sessionId = null;
    renderRoomList();
    if (isThreadRoom && typeof renderThreadPanelBody === 'function') {
      renderThreadPanelBody();
    }
    if (isCurrentRoom) {
      setOrbState('idle', '취소됨');
      renderMessagesForCurrentRoom();
      updateSendButtonState();
    }
    if (typeof updateSubAgentChipsStatus === 'function') {
      updateSubAgentChipsStatus(targetRoom.id, 'error', '작업 취소됨', targetRoom.title);
    }
    if (typeof updateInlineChatChipsStatus === 'function') {
      updateInlineChatChipsStatus(targetRoom.id, 'failed', targetRoom.title);
      if (typeof updateAllInlineChatChips === 'function') {
        updateAllInlineChatChips();
      }
    }
    // 발주처 방의 사전 예약 슬롯(.msg-slot-placeholder) 및 우측 스레드 패널 취소 상태 즉시 반영
    if (targetRoom && targetRoom.id) {
      document.querySelectorAll(`.msg-slot-placeholder[data-child-room-id="${targetRoom.id}"]`).forEach(slotCard => {
        const feedEl = slotCard.querySelector('.slot-live-feed-text');
        if (feedEl) feedEl.textContent = '🛑 사용자에 의해 실행이 중단되었습니다.';
        const spinner = slotCard.querySelector('.spinner');
        if (spinner) spinner.style.display = 'none';
        const badgeEl = slotCard.querySelector('.slot-status-pill');
        if (badgeEl) {
          badgeEl.classList.remove('is-running', 'is-queued', 'is-done');
          badgeEl.classList.add('is-error');
          const labelEl = badgeEl.querySelector('.slot-status-label');
          if (labelEl) labelEl.textContent = '중단/취소';
        }
      });
      // 슬롯을 포함하는 모든 방의 메시지 상태 갱신
      if (typeof chatRooms !== 'undefined' && Array.isArray(chatRooms)) {
        let anySlotUpdated = false;
        chatRooms.forEach(rm => {
          if (rm && Array.isArray(rm.messages)) {
            rm.messages.forEach(m => {
              if (m && (m.bypassedFromRoomId === targetRoom.id || (Array.isArray(m.steps) && m.steps.some(s => s && (s.roomId === targetRoom.id || s.bypassedFromRoomId === targetRoom.id))))) {
                m.isError = true;
                if (!String(m.text || '').trim()) {
                  m.text = `🛑 **[${targetRoom.title || targetRoom.id}]** 하위 작업이 취소되었습니다.`;
                }
                anySlotUpdated = true;
              }
            });
          }
        });
        if (anySlotUpdated) {
          saveChatRoomsToStorage();
          if (currentRoomId && currentRoomId !== targetRoom.id) {
            renderMessagesForCurrentRoom();
          }
        }
      }
    }
  } else if (data.type === 'result') {
    if (typeof flushLiveStreamBuffer === 'function') {
      flushLiveStreamBuffer();
    }
    const replyText = data.text || data.result || '';
    const finalSteps = filterToolSteps((Array.isArray(data.steps) && data.steps.length > 0) ? data.steps : [...liveState.steps]);
    const remainingQueued = getQueuedMessagesCount(targetRoom);

    // 하위 에이전트 칩 실시간 상태 갱신 (화면 내 스피너 -> 완료/오류 아이콘)
    if (typeof updateSubAgentChipsStatus === 'function') {
      const isErr = Boolean(data.error || (replyText && (replyText.startsWith('⚠️ 오류') || replyText.includes('도구 실행 실패'))));
      const resStatus = isErr ? 'error' : (remainingQueued > 0 ? 'running' : 'done');
      const resReason = isErr ? (data.error || '오류 발생') : (remainingQueued > 0 ? '다음 질문 실행 중...' : '정상 완료');
      updateSubAgentChipsStatus(targetRoom.id, resStatus, resReason, targetRoom.title);

      if (data.bypassedFromRoomId) {
        const subStatus = data.isSuccess === false ? 'error' : 'done';
        const subReason = data.isSuccess === false ? (data.error || '오류 발생') : '정상 완료';
        updateSubAgentChipsStatus(data.bypassedFromRoomId, subStatus, subReason, data.subagentTitle || '');
      }

      if (Array.isArray(data.delegatedRooms)) {
        data.delegatedRooms.forEach(item => {
          if (item) {
            updateSubAgentChipsStatus(item.roomId, item.status || 'done', item.reason || '정상 완료', item.title, item.taskId);
          }
        });
      }
    }

    liveState.isRunning = remainingQueued > 0;

    // 바이패스 하위 작업 룸의 실시간 상태 리셋 (스피너 고착 방지)
    if (data.bypassedFromRoomId) {
      if (typeof roomLiveStates !== 'undefined' && roomLiveStates && roomLiveStates[data.bypassedFromRoomId]) {
        const bLive = roomLiveStates[data.bypassedFromRoomId];
        bLive.isRunning = false;
        bLive.feed = '';
        bLive.steps = [];
        bLive.streamText = '';
        bLive.startedAt = 0;
        bLive.queueCount = 0;
      }
      if (typeof latestMasterTasks !== 'undefined' && Array.isArray(latestMasterTasks)) {
        latestMasterTasks.forEach(t => {
          if (t && (t.roomId === data.bypassedFromRoomId || (data.taskId && t.id === data.taskId))) {
            t.status = data.isSuccess === false ? 'failed' : 'done';
            t.finishedAt = Date.now() / 1000;
          }
        });
      }
    }
    if (Array.isArray(data.delegatedRooms)) {
      data.delegatedRooms.forEach(item => {
        if (item && item.roomId) {
          const st = (item.status === 'error' || item.status === 'failed') ? 'failed' : (item.status || 'done');
          if (st !== 'running' && typeof roomLiveStates !== 'undefined' && roomLiveStates && roomLiveStates[item.roomId]) {
            const dLive = roomLiveStates[item.roomId];
            dLive.isRunning = false;
            dLive.feed = '';
            dLive.steps = [];
            dLive.streamText = '';
            dLive.startedAt = 0;
            dLive.queueCount = 0;
          }
          if (typeof latestMasterTasks !== 'undefined' && Array.isArray(latestMasterTasks)) {
            latestMasterTasks.forEach(t => {
              if (t && (t.roomId === item.roomId || (item.taskId && t.id === item.taskId))) {
                t.status = st;
              }
            });
          }
        }
      });
    }

    if (typeof updateInlineChatChipsStatus === 'function') {
      updateInlineChatChipsStatus(targetRoom.id, remainingQueued > 0 ? 'running' : 'done');
      if (data.bypassedFromRoomId) {
        updateInlineChatChipsStatus(data.bypassedFromRoomId, data.isSuccess === false ? 'failed' : 'done', data.subagentTitle || '');
      }
      if (Array.isArray(data.delegatedRooms)) {
        data.delegatedRooms.forEach(item => {
          if (item && item.roomId) {
            const st = (item.status === 'error' || item.status === 'failed') ? 'failed' : (item.status || 'done');
            updateInlineChatChipsStatus(item.roomId, st, item.title || '');
          }
        });
      }
      if (typeof updateAllInlineChatChips === 'function') {
        updateAllInlineChatChips();
      }
      if (Array.isArray(data.activeRuns) && typeof applyActiveRunsSnapshot === 'function') {
        applyActiveRunsSnapshot(data.activeRuns, true);
      }
      if (typeof loadMasterTasksFromServer === 'function') {
        loadMasterTasksFromServer();
      }
    }
    liveState.feed = remainingQueued > 0 ? '다음 대기열 질문 실행 준비 중…' : '';
    liveState.steps = [];
    liveState.streamText = '';
    liveState.startedAt = remainingQueued > 0 ? Date.now() : 0;
    liveState.queueCount = remainingQueued;
    syncLiveElapsedTimer();
    setAgentAvatarStatus(remainingQueued > 0 ? 'thinking' : 'idle', remainingQueued > 0 ? '다음 질문 준비 중…' : '대기 중');

    const wasCurrentRoom = (targetRoom.id === currentRoomId);
    if (data.sessionId) {
      const oldRoomId = targetRoom.id;
      targetRoom.sessionId = data.sessionId;
      targetRoom.isNewSession = false;
      if (data.parentAgentId) {
        targetRoom.parentAgentId = data.parentAgentId;
      }
      const isOrchRoom = (typeof ORCHESTRATOR_ROOM_IDS !== 'undefined')
        ? ORCHESTRATOR_ROOM_IDS.includes(oldRoomId)
        : (oldRoomId === CORE_ROOM_ID || oldRoomId === '__expert_vault__' || oldRoomId === '__expert_coding__');
      if (!isOrchRoom) {
        if (!String(oldRoomId || '').startsWith('room-')) {
          targetRoom.id = data.sessionId;
          if (wasCurrentRoom) {
            currentRoomId = data.sessionId;
            if (typeof syncRoomHistory === 'function') {
              syncRoomHistory(currentRoomId, 'replace');
            }
          }
        }
        if (oldRoomId !== data.sessionId && roomLiveStates[oldRoomId]) {
          roomLiveStates[data.sessionId] = roomLiveStates[oldRoomId];
        }
      }
    }

    if (Array.isArray(targetRoom.messages) && targetRoom.messages.length > 0) {
      const lastIdx = targetRoom.messages.length - 1;
      const lastMsg = targetRoom.messages[lastIdx];
      const lastRole = lastMsg ? (lastMsg.sender || lastMsg.role || '') : '';
      const isLastSlot = Boolean(
        lastMsg && (
          lastMsg.channel === 'thread_bypass' ||
          lastMsg.isSlotPlaceholder ||
          (Array.isArray(lastMsg.steps) && lastMsg.steps.some(s => s && (s.kind === 'subagent_bypass' || s.tool === 'bypass')))
        )
      );
      if (lastRole === 'assistant' && !String(lastMsg.text || '').trim() && !isLastSlot) {
        targetRoom.messages.pop();
      }
    }

    const resolvedMsgId = data.msgId || data.slotMsgId || undefined;
    if (!resolvedMsgId && data.taskId && Array.isArray(targetRoom.messages)) {
      const slotByTask = targetRoom.messages.find(m => m && (m.slotTaskId === data.taskId || (Array.isArray(m.steps) && m.steps.some(s => s && s.taskId === data.taskId))));
      if (slotByTask && slotByTask.id) {
        data.msgId = slotByTask.id;
      }
    }

    upsertRoomMessage(targetRoom, {
      id: data.msgId || data.slotMsgId || undefined,
      role: 'assistant',
      sender: 'assistant',
      channel: data.turnType === 'delegation_result' ? 'thread_bypass' : (data.channel || undefined),
      text: replyText,
      steps: finalSteps,
      isSlotPlaceholder: false,
      slotTaskId: data.taskId || undefined,
      bypassedFromRoomId: data.bypassedFromRoomId || undefined,
      agentName: data.agentName || undefined,
      subagentTitle: data.subagentTitle || undefined,
      delegatedRooms: Array.isArray(data.delegatedRooms) ? data.delegatedRooms : undefined,
      model: data.model,
      effort: data.effort,
      elapsedSeconds: data.elapsedSeconds,
      createdAt: data.createdAt || Date.now()
    }, { beforeQueued: true, updateTimestamp: !data.inPlaceHydrated });
    targetRoom.messageCount = targetRoom.messages.length;
    targetRoom.updatedAt = Date.now();
    if (targetRoom.id === currentRoomId) {
      targetRoom.unread = false;
      if (Array.isArray(latestMasterTasks)) {
        latestMasterTasks.forEach(t => {
          if (t && t.roomId === targetRoom.id && !t.reviewed) {
            t.reviewed = true;
            t.reviewedAt = Date.now() / 1000;
            if (t.id) {
              fetch('/api/orchestrator/task/update', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ taskId: t.id, reviewed: true })
              }).catch(() => {});
            }
          }
        });
        if (typeof updateMasterTopbarBadge === 'function') updateMasterTopbarBadge();
      }
      fetch('/api/orchestrator/room/mark-read', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ roomId: targetRoom.id })
      }).catch(() => {});
    } else {
      targetRoom.unread = true;
    }
    sortChatRooms();
    saveChatRoomsToStorage();
    renderRoomList();
    if (isThreadRoom && typeof renderThreadPanelBody === 'function') {
      renderThreadPanelBody();
    }

    const notifyLevel = data.notifyLevel || (data.silentNotification ? 'silent' : 'alert');
    const isSilent = notifyLevel === 'silent' || Boolean(data.silentNotification);
    const isDelegationResult = data.turnType === 'delegation_result';
    const isChildRoom = Boolean(targetRoom && (targetRoom.isOrchestratorChild || targetRoom.parentAgentId));

    if (targetRoom.id === currentRoomId) {
      setOrbState(remainingQueued > 0 ? 'thinking' : 'idle', remainingQueued > 0 ? liveState.feed : '답변 완료');
      renderMessagesForCurrentRoom();
      updateSessionBar();
      updateSendButtonState();
    } else {
      if (!isSilent && (!isChildRoom || isDelegationResult)) {
        const toastTitle = isDelegationResult
          ? `[${targetRoom.title}] 하위 작업이 완료되었습니다.`
          : `[${targetRoom.title}] 답변이 도착했습니다.`;
        showToast(toastTitle, { roomId: targetRoom.id });
      }
    }

    // 브라우저 및 시스템 레벨 알림 (백그라운드/다른 탭/다른 방)
    // 상위 에이전트의 단순 스폰 안내(delegation_ack), 내부 보고(internal_report)는 침묵(silent)
    // 하위 작업 완료 바이패스(delegation_result) 또는 일반 직접 답변(direct_answer)만 알림 발송
    if (!isSilent && (!isChildRoom || isDelegationResult) && typeof handleAgentResponseNotification === 'function') {
      handleAgentResponseNotification(targetRoom, replyText);
    }

    const ttsMode = getTtsMode();
    const wasVoiceTurn = Boolean(data.voiceMode);
    // 스폰 안내나 silent 메시지는 TTS 음성 낭독도 억제
    const shouldPlayTts = !isSilent && (ttsMode === 'always' || (ttsMode === 'voice_only' && wasVoiceTurn));

    if (isCurrentRoom && data.speechText && shouldPlayTts) {
      playTts(data.speechText);
    }
  } else if (data.type === 'error') {
    const errMsg = '⚠️ ' + (data.message || data.error || '실행 중 오류가 발생했습니다.');
    const remainingQueued = getQueuedMessagesCount(targetRoom);
    liveState.isRunning = remainingQueued > 0;
    if (typeof updateInlineChatChipsStatus === 'function') {
      updateInlineChatChipsStatus(targetRoom.id, remainingQueued > 0 ? 'running' : 'failed');
    }
    liveState.feed = remainingQueued > 0 ? '다음 대기열 질문 실행 준비 중…' : '';
    liveState.steps = [];
    liveState.streamText = '';
    liveState.startedAt = remainingQueued > 0 ? Date.now() : 0;
    liveState.queueCount = remainingQueued;
    syncLiveElapsedTimer();
    setAgentAvatarStatus('idle', '오류 발생');

    upsertRoomMessage(targetRoom, {
      id: data.msgId || undefined,
      sender: 'assistant',
      role: 'assistant',
      text: errMsg,
      steps: [],
      isError: true,
      createdAt: data.createdAt || Date.now()
    }, { beforeQueued: true, updateTimestamp: true });
    saveChatRoomsToStorage();
    renderRoomList();

    const notifyLevel = data.notifyLevel || (data.silentNotification ? 'silent' : 'alert');
    const isSilent = notifyLevel === 'silent' || Boolean(data.silentNotification);
    const isDelegationResult = data.turnType === 'delegation_result';
    const isChildRoom = Boolean(targetRoom && (targetRoom.isOrchestratorChild || targetRoom.parentAgentId));

    if (isCurrentRoom) {
      setOrbState('idle', '오류 발생');
      renderMessagesForCurrentRoom();
      updateSendButtonState();
    } else {
      if (!isSilent && (!isChildRoom || isDelegationResult)) {
        showToast(`[${targetRoom.title}] 실행 오류가 발생했습니다.`, { roomId: targetRoom.id });
      }
    }
    if (!isSilent && (!isChildRoom || isDelegationResult) && typeof handleAgentResponseNotification === 'function') {
      handleAgentResponseNotification(targetRoom, errMsg, { isError: true });
    }
  }
}
