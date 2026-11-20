// ─── 대화방 스토리지 & 서버 동기화 (SQLite chat.db 단일 SSOT) ───
function scheduleServerBackup() {
  // SQLite DB 단일 진실 원천(SSOT) 원칙에 따라 브라우저 캐시의 서버 역동기화를 완전 비활성화
}

function getRoomTimestamp(r) {
  if (!r) return 0;
  const ts = r.updatedAt || r.createdAt || 0;
  if (typeof ts === 'number') return ts < 1e11 ? ts * 1000 : ts;
  const parsed = parseFloat(ts);
  if (!isNaN(parsed)) return parsed < 1e11 ? parsed * 1000 : parsed;
  const dt = Date.parse(ts);
  return isNaN(dt) ? 0 : dt;
}

function ensureCoreRoom() {
  const ensureAgent = (id, title, desc, workdir) => {
    let room = chatRooms.find(r => r && r.id === id);
    if (!room) {
      room = {
        id,
        title,
        preview: desc,
        messageCount: 0,
        messages: [],
        isNewSession: true,
        sessionId: null,
        workdir: workdir || (serverStatus && (serverStatus.vaultDir || serverStatus.workdir)) || '',
        updatedAt: 0
      };
      chatRooms.push(room);
    } else {
      room.title = title;
      if (!room.workdir && serverStatus) {
        room.workdir = workdir || serverStatus.vaultDir || serverStatus.workdir || '';
      }
    }
    return room;
  };

  const coreRoom = ensureAgent(CORE_ROOM_ID, CORE_ROOM_TITLE, '전체 AGY 세션 열람 · 지시 · 스폰', (serverStatus && (serverStatus.vaultDir || serverStatus.workdir)) || '');
  ensureAgent(EXPERT_VAULT_ROOM_ID, EXPERT_VAULT_TITLE, '옵시디언 볼트 지식 · 개인 기록 전담', (serverStatus && (serverStatus.vaultDir || serverStatus.workdir)) || '');
  ensureAgent(EXPERT_CODING_ROOM_ID, EXPERT_CODING_TITLE, 'oshyun-bot 개발 · 테스트 전담', (serverStatus && (serverStatus.codeDir || serverStatus.codedir || serverStatus.vaultDir)) || '');
  return coreRoom;
}

function updateCoreAgentSlotUi() {
  ensureCoreRoom();
  const updateSlot = (slotId, subId, roomId) => {
    const slot = document.getElementById(slotId);
    const subEl = document.getElementById(subId);
    if (!slot) return;
    const live = roomLiveStates[roomId];
    const isRunning = Boolean(live && live.isRunning);
    slot.classList.toggle('active', currentRoomId === roomId && currentView === 'chat');
    slot.classList.toggle('running', isRunning);
    if (subEl) {
      if (isRunning) {
        subEl.textContent = '작업 중';
      } else {
        subEl.textContent = '대기';
      }
    }
  };

  updateSlot('core-agent-slot', 'core-agent-sub', CORE_ROOM_ID);
  updateSlot('expert-vault-slot', 'expert-vault-sub', EXPERT_VAULT_ROOM_ID);
  updateSlot('expert-coding-slot', 'expert-coding-sub', EXPERT_CODING_ROOM_ID);
}

function getOrchestratorRoom(roomId) {
  ensureCoreRoom();
  const rid = roomId || CORE_ROOM_ID;
  return chatRooms.find(r => r && r.id === rid) || ensureCoreRoom();
}

function applyOrchestratorSessionResetLocal(targetRoomId, data = {}) {
  const rid = targetRoomId || CORE_ROOM_ID;
  const room = getOrchestratorRoom(rid);
  if (room) {
    room.messages = [];
    room.messageCount = 0;
    if (rid === EXPERT_VAULT_ROOM_ID) {
      room.preview = '옵시디언 볼트 지식 · 개인 기록 전담';
    } else if (rid === EXPERT_CODING_ROOM_ID) {
      room.preview = 'oshyun-bot 개발 · 테스트 전담';
    } else {
      room.preview = '전체 AGY 세션 열람 · 지시 · 스폰';
    }
    room.sessionId = null;
    room.isNewSession = true;
  }
  delete roomLiveStates[rid];
  if (rid === CORE_ROOM_ID && data && data.memory) {
    latestMasterMemory = data.memory;
    if (latestMasterGraph) latestMasterGraph.memory = data.memory;
    renderMsbMemory(data.memory);
  }
  saveChatRoomsToStorage();
  renderRoomList();
  if (currentRoomId === rid) {
    renderMessagesForCurrentRoom(true);
    updateSessionBar();
  }
  updateCoreAgentSlotUi();
}

function applyMasterSessionResetLocal(data = {}) {
  applyOrchestratorSessionResetLocal(CORE_ROOM_ID, data);
}

async function resetCoreOrchestratorRoom(event, targetRoomId = CORE_ROOM_ID) {
  if (event && typeof event.stopPropagation === 'function') event.stopPropagation();
  const targetId = targetRoomId || CORE_ROOM_ID;
  const isVault = targetId === EXPERT_VAULT_ROOM_ID;
  const isCoding = targetId === EXPERT_CODING_ROOM_ID;
  const agentScope = isVault ? 'vault' : (isCoding ? 'coding' : 'master');
  const agentLabel = isVault ? '세컨브레인' : (isCoding ? '봇개발' : '마스터');

  const doReset = async (clearDoneTasks = false) => {
    applyOrchestratorSessionResetLocal(targetId, {});
    try {
      const res = await fetch('/api/orchestrator/reset', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          roomId: targetId,
          agentId: agentScope,
          memoryMode: 'keep',
          clearDoneTasks: Boolean(clearDoneTasks),
        })
      });
      const data = await res.json();
      if (data && data.ok) {
        applyOrchestratorSessionResetLocal(targetId, data);
        if (targetId === CORE_ROOM_ID) {
          await loadMasterGraphFromServer();
          await loadMasterTasksFromServer();
        }
        showToast(`${agentLabel} 에이전트 대화가 초기화되었고 4계층 메모리는 그대로 유지되었습니다.`);
      } else {
        showToast(`${agentLabel} 초기화 실패: ` + ((data && data.error) || '알 수 없는 오류'));
      }
    } catch (err) {
      showToast(`${agentLabel} 초기화 요청 실패: ` + err.message);
    }
  };

  const extraHtml = (targetId === CORE_ROOM_ID) ? `
    <div style="display:flex;flex-direction:column;gap:8px;padding:10px 12px;border:1px solid var(--border);border-radius:8px;background:var(--bg-card);font-size:12.5px;color:var(--text-1);">
      <label style="display:flex;align-items:center;gap:8px;cursor:pointer;">
        <input type="checkbox" id="chk-reset-clear-tasks">
        <span>완료/실패/종결된 위임 작업 원장도 함께 정리</span>
      </label>
    </div>
  ` : '';
  openConfirmModal(
    `${agentLabel} 에이전트 초기화`,
    `${agentLabel} 에이전트(${targetId})의 대화 내역과 세션을 초기화하시겠습니까? (4계층 메모리는 유지됩니다)`,
    '초기화',
    () => {
      const clearTasksEl = document.getElementById('chk-reset-clear-tasks');
      doReset(Boolean(clearTasksEl && clearTasksEl.checked));
    },
    extraHtml
  );
}

function sortChatRooms() {
  chatRooms.sort((a, b) => {
    const pinA = a && a.pinned ? 1 : 0;
    const pinB = b && b.pinned ? 1 : 0;
    if (pinA !== pinB) return pinB - pinA;
    return getRoomTimestamp(b) - getRoomTimestamp(a);
  });
}

function deduplicateMessagesList(msgs) {
  if (!Array.isArray(msgs) || msgs.length <= 1) return Array.isArray(msgs) ? msgs : [];
  const out = [];
  const seenIds = new Map();
  const seenClientIds = new Map();
  for (const m of msgs) {
    if (!m || typeof m !== 'object') continue;
    const hasId = (m.id !== undefined && m.id !== null && m.id !== '');
    if (hasId) {
      if (seenIds.has(m.id)) {
        const idx = seenIds.get(m.id);
        if (idx >= 0 && idx < out.length && out[idx]) {
          out[idx] = { ...out[idx], ...m };
        }
        continue;
      }
      seenIds.set(m.id, out.length);
    }
    if (m.clientMsgId) {
      if (seenClientIds.has(m.clientMsgId)) {
        const existingIdx = seenClientIds.get(m.clientMsgId);
        if (existingIdx >= 0 && existingIdx < out.length && out[existingIdx]) {
          const prev = out[existingIdx];
          const preferNewText = String(m.text || '').trim().length >= String(prev.text || '').trim().length;
          out[existingIdx] = {
            ...prev,
            ...m,
            text: preferNewText ? m.text : prev.text,
          };
        }
        continue;
      }
      seenClientIds.set(m.clientMsgId, out.length);
    }
    if (m.queued) {
      out.push(m);
      continue;
    }
    const role = m.sender || m.role || 'assistant';
    const text = String(m.text || '').trim();
    const prev = out[out.length - 1];
    if (prev && !prev.queued) {
      const prevRole = prev.sender || prev.role || 'assistant';
      const prevText = String(prev.text || '').trim();
      const prevHasId = (prev.id !== undefined && prev.id !== null && prev.id !== '');
      if (prevRole === role && prevText === text && (!prevHasId || !hasId || prev.id === m.id)) {
        if (!prevHasId && hasId) {
          out[out.length - 1] = { ...prev, ...m };
          seenIds.set(m.id, out.length - 1);
        }
        continue;
      }
      // 이전 메시지가 빈 텍스트 플레이스홀더 어시스턴트이고 현재 메시지가 내용 있는 어시스턴트이면 대체
      if (prevRole === 'assistant' && role === 'assistant' && !prevText && text) {
        out[out.length - 1] = m;
        if (hasId) seenIds.set(m.id, out.length - 1);
        continue;
      }
      // 이전 메시지가 내용 있는 어시스턴트인데 현재 메시지가 빈 텍스트 플레이스홀더이면 무시
      if (prevRole === 'assistant' && role === 'assistant' && prevText && !text) {
        continue;
      }
    }
    out.push(m);
  }
  return out;
}

function saveChatRoomsToStorage(_skipBackup = false) {
  try {
    sortChatRooms();
    localStorage.removeItem(STORAGE_KEY_ROOMS);
    localStorage.removeItem(STORAGE_KEY_DELETED_ROOMS);
    if (currentRoomId) {
      localStorage.setItem(STORAGE_KEY_CURRENT_ROOM, currentRoomId);
    }
  } catch (_) {}
}

function loadChatRoomsFromStorage() {
  try {
    localStorage.removeItem(STORAGE_KEY_ROOMS);
    localStorage.removeItem(STORAGE_KEY_DELETED_ROOMS);
    const savedCur = localStorage.getItem(STORAGE_KEY_CURRENT_ROOM);
    if (savedCur) {
      currentRoomId = savedCur;
    }
  } catch (_) {}
  chatRooms = [];
  return false;
}

async function loadChatRoomsFromServer(autoSelectFirst = false, _forceServerSync = false) {
  try {
    const res = await fetch('/api/chat/rooms');
    const data = await res.json();
    if (data.ok && Array.isArray(data.rooms)) {
      // 1. 사용자가 직접 '새 대화' 버튼을 눌러 생성한 임시 빈 방, 현재 활성 미전송 방 및 마스터가 생성한 신규 방 보존
      const freshUnsentRooms = chatRooms.filter(
        r => r && !ORCHESTRATOR_ROOM_IDS.includes(r.id) && (
          ((r.userCreatedEmpty === true || r.id === currentRoomId) && (r.isNewSession === true || !r.sessionId) && (!r.messages || r.messages.length === 0)) ||
          (r.byOrchestrator === true && (!r.messages || r.messages.length === 0))
        )
      );

      // 2. 기존 로컬 방 캐시(메시지 및 상태 메타데이터) 보존 맵
      const localRoomMap = new Map();
      for (const r of chatRooms) {
        if (!r) continue;
        const rid = String(r.id || '').trim();
        const sid = String(r.sessionId || '').trim();
        if (rid) localRoomMap.set(rid, r);
        if (sid) localRoomMap.set(sid, r);
      }

      // 3. 오직 서버 SQLite DB(data.rooms)만을 단일 진실 원천(SSOT)으로 대화방 목록 구성
      const newChatRooms = [];
      const seenCanonicalIds = new Set();

      for (const sRoom of data.rooms) {
        const sId = String(sRoom.id || sRoom.roomId || '').trim();
        const sSessionId = String(sRoom.sessionId || '').trim();
        if (!sId) continue;

        const canonId = ORCHESTRATOR_ROOM_IDS.includes(sId) ? sId : (sId || sSessionId);
        if (seenCanonicalIds.has(canonId)) continue;
        seenCanonicalIds.add(canonId);

        const prevMeta = localRoomMap.get(sId) || (sSessionId ? localRoomMap.get(sSessionId) : null);
        let msgs = Array.isArray(sRoom.messages) && sRoom.messages.length > 0 ? sRoom.messages : [];
        const cachedMsgs = prevMeta && Array.isArray(prevMeta.messages) ? prevMeta.messages : null;
        if (cachedMsgs && cachedMsgs.length > msgs.length) {
          msgs = deduplicateMessagesList(cachedMsgs);
        }
        const serverMsgCount = typeof sRoom.messageCount === 'number' ? sRoom.messageCount : 0;
        const preservedHistoryLoaded = Boolean(
          prevMeta && prevMeta.historyLoaded && msgs.length >= serverMsgCount
        );

        let title = sRoom.title || '새 대화';
        if (canonId === CORE_ROOM_ID) title = CORE_ROOM_TITLE;
        else if (canonId === EXPERT_VAULT_ROOM_ID) title = EXPERT_VAULT_TITLE;
        else if (canonId === EXPERT_CODING_ROOM_ID) title = EXPERT_CODING_TITLE;
        newChatRooms.push({
          id: canonId,
          roomId: canonId,
          title: title,
          preview: sRoom.preview || '',
          messageCount: Math.max(msgs.length, serverMsgCount),
          updatedAt: sRoom.updatedAt || Date.now(),
          messages: msgs,
          sessionId: sSessionId || null,
          isNewSession: sSessionId ? false : Boolean(sRoom.isNewSession),
          isOrchestratorChild: Boolean(
            sRoom.isOrchestratorChild ||
            sRoom.lastChannel === 'orchestrator' ||
            (prevMeta && (prevMeta.isOrchestratorChild || prevMeta.byOrchestrator))
          ),
          parentAgentId: sRoom.parentAgentId || (prevMeta && prevMeta.parentAgentId) || null,
          historyLoaded: preservedHistoryLoaded,
          pinned: Boolean(sRoom.pinned),
          isArchived: Boolean(sRoom.isArchived),
          model: sRoom.model || null,
          effort: sRoom.effort || null,
          workdir: sRoom.workdir || null
        });
      }

      // 4. 사용자가 명시적으로 만든 신규 빈 방(현재 활성화 중이거나 오케스트레이터가 생성한 방) 복원
      const freshToRestore = freshUnsentRooms.filter(
        r => r && (r.id === currentRoomId || r.byOrchestrator) && !newChatRooms.some(nr => nr && nr.id === r.id)
      );
      if (freshToRestore.length > 0) {
        newChatRooms.unshift(...freshToRestore);
      }

      // 5. 방이 하나도 없으면 기본 빈 방 1개 생성
      if (newChatRooms.length === 0) {
        const defaultId = 'room-' + Date.now();
        newChatRooms.push({
          id: defaultId,
          roomId: defaultId,
          title: '새 대화',
          preview: '',
          messageCount: 0,
          messages: [],
          isNewSession: true,
          userCreatedEmpty: true,
          sessionId: null,
          updatedAt: Date.now()
        });
      }

      chatRooms = newChatRooms;
      // 마스터 및 전문가 에이전트(ORCHESTRATOR_ROOM_IDS) 방은 항상 chatRooms에 상시 보장
      ensureCoreRoom();
      sortChatRooms();
      saveChatRoomsToStorage(true);

      const isOrchRoom = Boolean(currentRoomId && (typeof ORCHESTRATOR_ROOM_IDS !== 'undefined' ? ORCHESTRATOR_ROOM_IDS : [CORE_ROOM_ID]).includes(currentRoomId));
      if (
        !currentRoomId ||
        (currentRoomId !== CORE_ROOM_ID && !isOrchRoom && !chatRooms.some(r => r.id === currentRoomId || (r.sessionId && r.sessionId === currentRoomId))) ||
        autoSelectFirst
      ) {
        if (isOrchRoom && !autoSelectFirst) {
          // 오케스트레이터(마스터/전문가) 룸 컨텍스트는 임의로 첫 방으로 fallback되지 않고 완벽 보존
        } else if (chatRooms.length > 0) {
          currentRoomId = chatRooms[0].id;
        }
      } else if (currentRoomId !== CORE_ROOM_ID && !isOrchRoom) {
        const matchedCur = chatRooms.find(r => r.id === currentRoomId || (r.sessionId && r.sessionId === currentRoomId));
        if (matchedCur) currentRoomId = matchedCur.id;
      }

      if (currentRoomId && typeof syncRoomHistory === 'function') {
        syncRoomHistory(currentRoomId, 'replace');
      }

      if (Array.isArray(data.activeRuns)) {
        applyActiveRunsSnapshot(data.activeRuns, true);
      }

      renderRoomList();
      updateSessionBar({ skipMasterUi: true });
      syncMasterModeUi({ loadData: true });

      const cur = getCurrentRoom();
      renderMessagesForCurrentRoom(true);
      if (cur && !cur.userCreatedEmpty) {
        await loadChatHistoryFromServer(cur.id, cur.sessionId);
      }
      focusChatInput();
    }
  } catch (_) {}
}

async function syncRoomsWithServer() {
  const syncBtn = document.getElementById('btn-sync-rooms');
  if (syncBtn) syncBtn.classList.add('spinning');

  // 진행 중인 모든 대화방의 실시간 실행/스트리밍/도구호출(activeSteps) 상태 안전 백업
  const liveBackup = {};
  for (const [rid, st] of Object.entries(roomLiveStates)) {
    if (st && (st.isRunning || (Array.isArray(st.steps) && st.steps.length > 0) || Boolean(st.streamText))) {
      liveBackup[rid] = {
        isRunning: Boolean(st.isRunning),
        jobId: st.jobId || '',
        asstMsgId: st.asstMsgId || null,
        feed: st.feed || '',
        steps: Array.isArray(st.steps) ? st.steps.map(s => (s && typeof s === 'object') ? { ...s } : s) : [],
        streamText: st.streamText || '',
        startedAt: st.startedAt || 0,
        queueCount: typeof st.queueCount === 'number' ? st.queueCount : 0,
        model: st.model || null,
        effort: st.effort || null
      };
    }
  }

  try {
    await loadChatRoomsFromServer(false, true);

    // 서버 동기화 완료 후 백업된 실시간 실행 상태(activeSteps 등)가 유실/축소되지 않도록 복원·병합
    for (const [rid, bSt] of Object.entries(liveBackup)) {
      const curSt = ensureRoomLiveState(rid);
      if (bSt.isRunning) curSt.isRunning = true;
      if (bSt.jobId && !curSt.jobId) curSt.jobId = bSt.jobId;
      if (bSt.asstMsgId && !curSt.asstMsgId) curSt.asstMsgId = bSt.asstMsgId;
      if (bSt.startedAt && (!curSt.startedAt || bSt.startedAt < curSt.startedAt)) curSt.startedAt = bSt.startedAt;
      if (bSt.model && !curSt.model) curSt.model = bSt.model;
      if (bSt.effort && !curSt.effort) curSt.effort = bSt.effort;
      if (bSt.feed && (!curSt.feed || curSt.feed === '생각하는 중…')) curSt.feed = bSt.feed;

      if (Array.isArray(bSt.steps) && bSt.steps.length > 0) {
        curSt.steps = mergeToolSteps(bSt.steps, curSt.steps || []);
      }
      if (bSt.streamText) {
        const curLen = String(curSt.streamText || '').length;
        if (bSt.streamText.length >= curLen) {
          curSt.streamText = bSt.streamText;
        }
      }
      if (typeof bSt.queueCount === 'number' && bSt.queueCount > (curSt.queueCount || 0)) {
        curSt.queueCount = bSt.queueCount;
      }
    }

    syncLiveElapsedTimer();
    const curRoom = getCurrentRoom();
    if (curRoom && roomLiveStates[curRoom.id] && roomLiveStates[curRoom.id].isRunning) {
      renderMessagesForCurrentRoom(true);
      updateComposerStatusBar();
      updateSendButtonState();
    }

    showToast('대화 목록을 서버와 동기화했습니다.');
  } catch (_) {
    showToast('동기화 실패');
  } finally {
    if (syncBtn) {
      setTimeout(() => syncBtn.classList.remove('spinning'), 500);
    }
  }
}

function resetLocalChatCacheAndSync() {
  openConfirmModal(
    '로컬 캐시 초기화',
    '브라우저에 저장된 대화방 캐시를 비우고 서버의 최신 상태로 새로고침하시겠습니까?',
    '초기화',
    async () => {
      try {
        localStorage.removeItem(STORAGE_KEY_ROOMS);
        localStorage.removeItem(STORAGE_KEY_CURRENT_ROOM);
        localStorage.removeItem(STORAGE_KEY_DELETED_ROOMS);
      } catch (_) {}
      chatRooms = [];
      currentRoomId = '';
      await loadChatRoomsFromServer(true, true);
      showToast('로컬 캐시를 초기화하고 서버와 동기화했습니다.');
    }
  );
}

const _historyFetchSeq = {};
async function loadChatHistoryFromServer(roomId, sessionId) {
  const targetId = roomId || sessionId;
  if (!targetId) return;
  const reqSeq = (_historyFetchSeq[targetId] = (_historyFetchSeq[targetId] || 0) + 1);
  const reqStartedAt = Date.now();
  try {
    const url = '/api/chat/history?roomId=' + encodeURIComponent(roomId || '') +
                '&sessionId=' + encodeURIComponent(sessionId || '');
    const res = await fetch(url);
    const data = await res.json();
    if (_historyFetchSeq[targetId] !== reqSeq) return;
    const room = getRoomById(roomId) || getRoomById(sessionId);
    if (data.ok && Array.isArray(data.messages)) {
      if (room) {
        const serverMsgs = data.messages.filter(m => m && typeof m === 'object');
        if (Array.isArray(room.messages) && room.messages.length > 0) {
          const prevExpandedMap = new Map();
          room.messages.forEach(oldM => {
            if (oldM && oldM._expanded && oldM.id) {
              prevExpandedMap.set(oldM.id, true);
            }
          });
          if (prevExpandedMap.size > 0) {
            serverMsgs.forEach(sm => {
              if (sm && sm.id && prevExpandedMap.has(sm.id)) {
                sm._expanded = true;
              }
            });
          }
        }
        const serverIds = new Set(serverMsgs.map(m => m.id).filter(id => id !== undefined && id !== null && id !== ''));
        let maxServerId = 0;
        for (const sid of serverIds) {
          if (typeof sid === 'number' && sid > maxServerId) maxServerId = sid;
        }
        const serverRecentTail = serverMsgs.slice(-4);
        const liveRunUserId = data.liveRun ? data.liveRun.userMsgId : null;
        const liveRunClientId = data.liveRun ? data.liveRun.clientMsgId : null;
        const liveQueuedClientIds = new Set(
          (data.liveRun && Array.isArray(data.liveRun.queuedMessages) ? data.liveRun.queuedMessages : [])
            .map(qm => qm && qm.clientMsgId)
            .filter(Boolean)
        );
        if (liveRunUserId && liveRunClientId) {
          const matchedServerMsg = serverMsgs.find(sm => sm && sm.id === liveRunUserId);
          if (matchedServerMsg && !matchedServerMsg.clientMsgId) {
            matchedServerMsg.clientMsgId = liveRunClientId;
          }
        }
        const nowMs = Date.now();
        const localExtra = Array.isArray(room.messages)
          ? room.messages.filter(m => {
              if (!m || typeof m !== 'object') return false;
              const mHasId = (m.id !== undefined && m.id !== null && m.id !== '');
              if (mHasId) {
                return !serverIds.has(m.id) && typeof m.id === 'number' && m.id > maxServerId;
              }
              if (m.queued) {
                if (liveRunClientId && m.clientMsgId === liveRunClientId) return false;
                if (data.liveRun) {
                  if (m._localPendingAt && (nowMs - m._localPendingAt) < 3000) return true;
                  return Boolean(m.clientMsgId && liveQueuedClientIds.has(m.clientMsgId));
                }
                return Boolean(m._localPendingAt && (nowMs - m._localPendingAt) < 3000);
              }
              if (m.clientMsgId) {
                if (serverMsgs.some(sm => sm && (sm.clientMsgId === m.clientMsgId || (sm.id && sm.id === m.id)))) {
                  return false;
                }
                if (liveRunClientId && m.clientMsgId === liveRunClientId && liveRunUserId && serverIds.has(liveRunUserId)) {
                  return false;
                }
                const mRole = m.sender || m.role || 'user';
                const mText = String(m.text || '').trim();
                const alreadyInServerTail = serverRecentTail.some(
                  sm => (sm.sender || sm.role || '') === mRole && String(sm.text || '').trim() === mText
                );
                return !alreadyInServerTail;
              }
              return false;
            })
          : [];

        // 현재 대화방이 실행 중인 경우, DB에 미리 생성된 미완료 어시스턴트 레코드가 liveRow와 중복되어 도구단계를 가리지 않도록 제거
        const curLiveState = roomLiveStates[room.id] || (room.sessionId ? roomLiveStates[room.sessionId] : null);
        if (curLiveState && curLiveState.isRunning && serverMsgs.length > 0) {
          const lastSm = serverMsgs[serverMsgs.length - 1];
          if (lastSm && (lastSm.sender === 'assistant' || lastSm.role === 'assistant')) {
            const isPlaceholder = !String(lastSm.text || '').trim();
            const isMatchingAsst = Boolean(curLiveState.asstMsgId && lastSm.id === curLiveState.asstMsgId);
            if (isPlaceholder || isMatchingAsst) {
              serverMsgs.pop();
            }
          }
        }

        room.messages = deduplicateMessagesList([...serverMsgs, ...localExtra]);
        room.messageCount = room.messages.length;
        room.historyLoaded = true;
        if (data.messages.length > 0) room.isNewSession = false;
        if (sessionId) room.sessionId = sessionId;
        if (data.model !== undefined) room.model = data.model;
        if (data.effort !== undefined) room.effort = data.effort;
        if (data.workdir !== undefined) room.workdir = data.workdir;
        if (data.liveRun) {
          applyActiveRunsSnapshot([data.liveRun], false);
        }
        saveChatRoomsToStorage(true);
        if (room.id === currentRoomId) {
          renderMessagesForCurrentRoom(true);
          updateRoomConfigBar();
          focusChatInput();
        }
        renderRoomList();
      }
    } else if (room && room.id === currentRoomId) {
      if (data && data.liveRun) {
        applyActiveRunsSnapshot([data.liveRun], false);
      }
      renderMessagesForCurrentRoom(true);
    }
  } catch (err) {
    const room = getRoomById(roomId) || getRoomById(sessionId);
    if (room && room.id === currentRoomId) {
      renderMessagesForCurrentRoom(true);
    }
  }
}

function openConfirmModal(title, desc, okLabel, onConfirm, extraHtml = '') {
  const modal = document.getElementById('modal-confirm');
  const titleEl = document.getElementById('modal-confirm-title');
  const descEl = document.getElementById('modal-confirm-desc');
  const extraEl = document.getElementById('modal-confirm-extra');
  const okBtn = document.getElementById('modal-confirm-ok');
  if (!modal || !okBtn) {
    if (confirm(desc)) onConfirm();
    return;
  }
  if (titleEl) titleEl.innerText = title || '확인';
  if (descEl) descEl.innerText = desc || '';
  if (extraEl) {
    extraEl.innerHTML = extraHtml || '';
    extraEl.style.display = extraHtml ? 'block' : 'none';
  }
  okBtn.innerText = okLabel || '확인';
  confirmModalCallback = onConfirm;
  okBtn.onclick = () => {
    const cb = confirmModalCallback;
    try {
      if (typeof cb === 'function') cb();
    } finally {
      closeConfirmModal();
    }
  };
  modal.style.display = 'flex';
  setTimeout(() => {
    if (okBtn) okBtn.focus();
  }, 40);
}

function closeConfirmModal() {
  const modal = document.getElementById('modal-confirm');
  const extraEl = document.getElementById('modal-confirm-extra');
  if (extraEl) {
    extraEl.innerHTML = '';
    extraEl.style.display = 'none';
  }
  if (modal) modal.style.display = 'none';
  confirmModalCallback = null;
}

async function deleteRoom(e, roomId) {
  if (e && typeof e.stopPropagation === 'function') e.stopPropagation();
  if (!roomId || ORCHESTRATOR_ROOM_IDS.includes(roomId)) {
    showToast('전문가/마스터 에이전트 대화방은 삭제할 수 없습니다.');
    return;
  }
  const roomToDelete = chatRooms.find(r => r.id === roomId);
  const title = roomToDelete ? roomToDelete.title : '대화방';
  const isEmpty = !roomToDelete || ((!roomToDelete.messages || roomToDelete.messages.length === 0) && !(roomToDelete.messageCount > 0));
  if (!isEmpty) {
    openConfirmModal(
      '대화 삭제',
      `'${title}' 대화를 삭제하시겠습니까?`,
      '삭제',
      () => executeBatchDeleteRooms([roomId])
    );
    return;
  }
  await executeBatchDeleteRooms([roomId]);
}

async function executeBatchDeleteRooms(targetRoomIds) {
  const idSet = new Set((targetRoomIds || []).map(x => String(x || '').trim()).filter(Boolean));
  for (const orchId of ORCHESTRATOR_ROOM_IDS) {
    idSet.delete(orchId);
  }
  if (idSet.size === 0) return;

  const roomsToDelete = chatRooms.filter(r => r && (idSet.has(String(r.id)) || (r.sessionId && idSet.has(String(r.sessionId)))));
  const allRoomIds = new Set(idSet);
  const allSessionIds = new Set();

  for (const r of roomsToDelete) {
    if (r.id) allRoomIds.add(String(r.id));
    if (r.sessionId) allSessionIds.add(String(r.sessionId));
    else if (r.id) allSessionIds.add(String(r.id));
  }
  for (const rawId of idSet) {
    allSessionIds.add(rawId);
  }

  for (const rid of allRoomIds) {
    const liveState = roomLiveStates[rid];
    if (liveState && liveState.isRunning && socket && socket.readyState === WebSocket.OPEN) {
      try { socket.send(JSON.stringify({ type: 'cancel', roomId: rid })); } catch (_) {}
    }
    delete roomLiveStates[rid];
    selectedRoomIds.delete(rid);
  }

  const roomIdsArr = Array.from(allRoomIds);
  const sessionIdsArr = Array.from(allSessionIds);

  const wasCurrentDeleted = allRoomIds.has(String(currentRoomId)) || allSessionIds.has(String(currentRoomId));
  const sidebarWasOpen = !!document.getElementById('room-sidebar')?.classList.contains('open');
  chatRooms = chatRooms.filter(r => {
    if (!r) return false;
    const rid = String(r.id || '');
    const sid = String(r.sessionId || '');
    return !allRoomIds.has(rid) && !allSessionIds.has(rid) && (!sid || (!allRoomIds.has(sid) && !allSessionIds.has(sid)));
  });
  saveChatRoomsToStorage();

  const deletedCount = Math.max(roomsToDelete.length, idSet.size);
  if (isRoomSelectionMode) {
    exitRoomSelectionMode();
  }

  if (wasCurrentDeleted) {
    if (chatRooms.length > 0) {
      selectRoom(chatRooms[0].id, { keepDrawerOpen: sidebarWasOpen, historyMode: 'replace' });
    } else {
      createNewChat(true, { keepDrawerOpen: sidebarWasOpen, historyMode: 'replace' });
    }
  } else {
    renderRoomList();
  }
  if (sidebarWasOpen) {
    openRoomDrawer();
  }
  triggerHaptic(18);
  showToast(deletedCount > 1 ? `${deletedCount}개의 대화가 삭제되었습니다.` : '대화가 삭제되었습니다.');

  try {
    await fetch('/api/session/batch-delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        roomId: roomIdsArr[0] || '',
        sessionId: sessionIdsArr[0] || '',
        roomIds: roomIdsArr,
        sessionIds: sessionIdsArr
      })
    });
  } catch (_) {}
}

// ─── 대화방 길게 누르기 다중 선택 & 일괄 관리 ───
function syncRoomSelectionDom() {
  const sidebarEl = document.getElementById('room-sidebar');
  if (!sidebarEl) return false;
  const items = sidebarEl.querySelectorAll('.room-item[data-room-id]');
  if (items.length === 0) return false;
  items.forEach(el => {
    const rid = el.getAttribute('data-room-id');
    el.classList.toggle('selected', Boolean(rid && selectedRoomIds.has(rid)));
  });
  return true;
}

function enterRoomSelectionMode(initialRoomId = null) {
  closeSwipedRoom();
  isRoomSelectionMode = true;
  if (initialRoomId) {
    selectedRoomIds.add(String(initialRoomId));
  }
  triggerHaptic(25);
  document.getElementById('room-sidebar')?.classList.add('select-mode');
  const btn = document.getElementById('btn-room-select-mode');
  if (btn) {
    btn.classList.add('active');
    btn.innerText = '취소';
  }
  updateRoomSelectionUi();
  if (!syncRoomSelectionDom()) renderRoomList();
}

function exitRoomSelectionMode() {
  isRoomSelectionMode = false;
  selectedRoomIds.clear();
  document.getElementById('room-sidebar')?.classList.remove('select-mode');
  const btn = document.getElementById('btn-room-select-mode');
  if (btn) {
    btn.classList.remove('active');
    btn.innerText = '선택';
  }
  updateRoomSelectionUi();
  if (!syncRoomSelectionDom()) renderRoomList();
}

function toggleRoomSelectionMode() {
  if (isRoomSelectionMode) exitRoomSelectionMode();
  else enterRoomSelectionMode();
}

function toggleRoomSelection(roomId, event) {
  if (event && typeof event.stopPropagation === 'function') event.stopPropagation();
  const id = String(roomId || '').trim();
  if (!id) return;
  if (!isRoomSelectionMode) {
    enterRoomSelectionMode(id);
    return;
  }
  if (selectedRoomIds.has(id)) selectedRoomIds.delete(id);
  else selectedRoomIds.add(id);
  triggerHaptic(12);
  updateRoomSelectionUi();
  if (!syncRoomSelectionDom()) renderRoomList();
}

function extractLocalMatchSnippet(text, query, radius = 32) {
  const clean = String(text || '').replace(/\s+/g, ' ').trim();
  if (!clean) return '';
  const idx = clean.toLowerCase().indexOf(query.toLowerCase());
  if (idx < 0) return clean.slice(0, 80);
  const start = Math.max(0, idx - radius);
  const end = Math.min(clean.length, idx + query.length + radius);
  let snippet = clean.slice(start, end);
  if (start > 0) snippet = '…' + snippet;
  if (end < clean.length) snippet = snippet + '…';
  return snippet;
}

function getFilteredUniqueRooms() {
  sortChatRooms();
  const seenIds = new Set();
  const uniqueRooms = [];
  const q = (roomSearchQuery || '').trim().toLowerCase();
  for (const r of chatRooms) {
    if (!r) continue;
    const rid = String(r.id || '').trim();
    if (rid === CORE_ROOM_ID || ORCHESTRATOR_ROOM_IDS.includes(rid)) continue;
    let sid = String(r.sessionId || '').trim();
    if (sid && ORCHESTRATOR_ROOM_IDS.includes(sid)) continue;
    if (sid === rid) sid = '';

    if ((rid && seenIds.has(rid)) || (sid && seenIds.has(sid))) continue;
    if (rid) seenIds.add(rid);
    if (sid) seenIds.add(sid);

    r._searchSnippet = '';
    if (q) {
      const titleMatch = (r.title || '').toLowerCase().includes(q);
      const previewMatch = (r.preview || '').toLowerCase().includes(q);
      const matchedLocalMsg = Array.isArray(r.messages)
        ? r.messages.find(m => (m.text || '').toLowerCase().includes(q))
        : null;
      const serverMatch = roomServerSearchResults
        ? (roomServerSearchResults.get(rid) || (sid ? roomServerSearchResults.get(sid) : null))
        : null;
      if (!titleMatch && !previewMatch && !matchedLocalMsg && !serverMatch) continue;
      if (serverMatch && serverMatch.matchSnippet) {
        r._searchSnippet = serverMatch.matchSnippet;
      } else if (matchedLocalMsg) {
        r._searchSnippet = extractLocalMatchSnippet(matchedLocalMsg.text, q);
      } else if (previewMatch && !titleMatch) {
        r._searchSnippet = extractLocalMatchSnippet(r.preview, q);
      }
    }
    uniqueRooms.push(r);
  }

  // 초기 목록(최근 50개)에 없던 과거 대화방도 서버 본문 검색 결과에 있으면 포함
  if (q && roomServerSearchResults) {
    for (const sRoom of roomServerSearchResults.values()) {
      if (!sRoom) continue;
      const rid = String(sRoom.id || sRoom.roomId || '').trim();
      if (!rid || rid === CORE_ROOM_ID || ORCHESTRATOR_ROOM_IDS.includes(rid)) continue;
      let sid = String(sRoom.sessionId || '').trim();
      if (sid && ORCHESTRATOR_ROOM_IDS.includes(sid)) continue;
      if (sid === rid) sid = '';
      if (seenIds.has(rid) || (sid && seenIds.has(sid))) continue;
      seenIds.add(rid);
      if (sid) seenIds.add(sid);
      uniqueRooms.push({
        id: rid,
        roomId: rid,
        title: sRoom.title || '새 대화',
        preview: sRoom.preview || '',
        sessionId: sid,
        createdAt: sRoom.createdAt,
        updatedAt: sRoom.updatedAt,
        lastChannel: sRoom.lastChannel || 'web',
        isArchived: Boolean(sRoom.isArchived),
        isOrchestratorChild: Boolean(sRoom.isOrchestratorChild),
        parentAgentId: sRoom.parentAgentId || null,
        model: sRoom.model || null,
        effort: sRoom.effort || null,
        workdir: sRoom.workdir || null,
        messageCount: sRoom.messageCount || 0,
        messages: [],
        _searchSnippet: sRoom.matchSnippet || '',
      });
    }
  }
  return uniqueRooms;
}

function toggleSelectAllRooms() {
  const visible = getFilteredUniqueRooms();
  if (visible.length === 0) return;
  const allSelected = visible.every(r => selectedRoomIds.has(String(r.id)));
  if (allSelected) {
    visible.forEach(r => selectedRoomIds.delete(String(r.id)));
  } else {
    visible.forEach(r => selectedRoomIds.add(String(r.id)));
  }
  triggerHaptic(15);
  updateRoomSelectionUi();
  if (!syncRoomSelectionDom()) renderRoomList();
}

function updateRoomSelectionUi() {
  const count = selectedRoomIds.size;
  const countEl = document.getElementById('room-selected-count');
  if (countEl) countEl.innerText = `${count}개 선택됨`;

  const visible = getFilteredUniqueRooms();
  const selectAllBtn = document.getElementById('btn-select-all-rooms');
  if (selectAllBtn) {
    const allSelected = visible.length > 0 && visible.every(r => selectedRoomIds.has(String(r.id)));
    selectAllBtn.innerText = allSelected ? '선택 해제' : '전체 선택';
  }

  const pinBtn = document.getElementById('btn-bulk-pin');
  const renameBtn = document.getElementById('btn-bulk-rename');
  const deleteBtn = document.getElementById('btn-bulk-delete');
  const deleteLabel = document.getElementById('btn-bulk-delete-label');

  if (pinBtn) pinBtn.disabled = count === 0;
  if (renameBtn) renameBtn.disabled = count !== 1;
  if (deleteBtn) deleteBtn.disabled = count === 0;
  if (deleteLabel) deleteLabel.innerText = count > 0 ? `삭제 (${count})` : '삭제';
}

function deleteSelectedRooms() {
  const ids = Array.from(selectedRoomIds);
  if (ids.length === 0) return;
  const desc = ids.length === 1
    ? '선택한 1개의 대화를 삭제하시겠습니까?'
    : `선택한 ${ids.length}개의 대화를 한 번에 삭제하시겠습니까? 삭제된 대화는 복구할 수 없습니다.`;
  openConfirmModal(
    `${ids.length}개 대화 일괄 삭제`,
    desc,
    `${ids.length}개 삭제`,
    () => executeBatchDeleteRooms(ids)
  );
}

function togglePinSelectedRooms() {
  const ids = Array.from(selectedRoomIds);
  if (ids.length === 0) return;
  const targets = chatRooms.filter(r => r && selectedRoomIds.has(String(r.id)));
  if (targets.length === 0) return;
  const shouldPin = targets.some(r => !r.pinned);
  targets.forEach(r => { r.pinned = shouldPin; });
  saveChatRoomsToStorage();
  exitRoomSelectionMode();
  showToast(shouldPin ? `${targets.length}개 대화를 상단에 고정했습니다.` : '상단 고정을 해제했습니다.');
}

function openRenameSelectedRoomModal() {
  const ids = Array.from(selectedRoomIds);
  if (ids.length !== 1) return;
  openRenameRoomModal(ids[0]);
}

function openRenameRoomModal(roomId) {
  const room = getRoomById(roomId || currentRoomId);
  if (!room) return;
  renamingRoomId = room.id;
  const modal = document.getElementById('modal-rename-room');
  const input = document.getElementById('input-rename-room');
  if (input) input.value = room.title || '';
  if (modal) modal.style.display = 'flex';
  setTimeout(() => {
    if (input) {
      try { input.focus({ preventScroll: true }); } catch (_) { input.focus(); }
      input.select();
    }
  }, 40);
}

function closeRenameRoomModal() {
  const modal = document.getElementById('modal-rename-room');
  if (modal) modal.style.display = 'none';
  renamingRoomId = null;
}

async function submitRenameRoom() {
  const input = document.getElementById('input-rename-room');
  const newTitle = (input ? input.value : '').trim();
  if (!newTitle) {
    showToast('대화 제목을 입력해 주세요.');
    return;
  }
  const room = getRoomById(renamingRoomId || currentRoomId);
  if (!room) {
    closeRenameRoomModal();
    return;
  }
  room.title = newTitle;
  room.updatedAt = Date.now();
  saveChatRoomsToStorage();
  closeRenameRoomModal();
  if (isRoomSelectionMode) exitRoomSelectionMode();
  else renderRoomList();
  updateTopbarTitle();

  fetch('/api/chat/room/rename', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      roomId: room.id,
      sessionId: room.sessionId || room.id,
      title: newTitle
    })
  }).catch(() => {});
  showToast('대화 이름이 변경되었습니다.');
}

function onTopbarTitleClick() {
  if (currentView !== 'chat' || currentRoomId === CORE_ROOM_ID) return;
  openRenameRoomModal(currentRoomId);
}

function onRoomSearchInput() {
  const input = document.getElementById('room-search-input');
  roomSearchQuery = input ? input.value : '';
  const trimmed = roomSearchQuery.trim();
  const clearBtn = document.getElementById('btn-clear-room-search');
  if (clearBtn) clearBtn.classList.toggle('show', Boolean(trimmed));

  if (roomSearchDebounceTimer) {
    clearTimeout(roomSearchDebounceTimer);
    roomSearchDebounceTimer = null;
  }
  roomSearchSeq += 1;
  const mySeq = roomSearchSeq;

  if (!trimmed) {
    roomServerSearchResults = null;
    renderRoomList();
    if (isRoomSelectionMode) updateRoomSelectionUi();
    return;
  }

  // 1차: 로컬 캐시 기준 즉시 필터링
  renderRoomList();
  if (isRoomSelectionMode) updateRoomSelectionUi();

  // 2차: 서버 SQLite 전체 메시지 본문 검색 (디바운스)
  roomSearchDebounceTimer = setTimeout(async () => {
    try {
      const res = await fetch('/api/chat/rooms/search?q=' + encodeURIComponent(trimmed));
      const data = await res.json();
      if (mySeq !== roomSearchSeq || roomSearchQuery.trim() !== trimmed) return;
      if (data && data.ok && Array.isArray(data.rooms)) {
        const map = new Map();
        for (const r of data.rooms) {
          if (!r) continue;
          const rid = String(r.id || r.roomId || '').trim();
          const sid = String(r.sessionId || '').trim();
          if (rid) map.set(rid, r);
          if (sid && sid !== rid) map.set(sid, r);
        }
        roomServerSearchResults = map;
        renderRoomList();
        if (isRoomSelectionMode) updateRoomSelectionUi();
      }
    } catch (_) {}
  }, 180);
}

function clearRoomSearch() {
  const input = document.getElementById('room-search-input');
  if (input) input.value = '';
  roomSearchQuery = '';
  roomServerSearchResults = null;
  roomSearchSeq += 1;
  if (roomSearchDebounceTimer) {
    clearTimeout(roomSearchDebounceTimer);
    roomSearchDebounceTimer = null;
  }
  document.getElementById('btn-clear-room-search')?.classList.remove('show');
  renderRoomList();
  if (isRoomSelectionMode) updateRoomSelectionUi();
}

function closeModal(modalId) {
  if (!modalId) return;
  const modal = typeof modalId === 'string' ? document.getElementById(modalId) : modalId;
  if (modal) modal.style.display = 'none';
}

function onModalBackdropClick(event, modalId) {
  if (event.target && event.target.id === modalId) {
    closeModal(modalId);
  }
}

function setAgentAvatarStatus(state, snippet) {
  const dot = document.getElementById('avatar-status-dot');
  const snippetEl = document.getElementById('avatar-status-snippet');
  if (state === 'offline') {
    if (dot) dot.className = 'status-dot offline';
    if (snippetEl) snippetEl.innerText = snippet || '재연결 중…';
    return;
  }
  const runningCount = Object.values(roomLiveStates).filter(s => s && s.isRunning).length;
  if (runningCount > 0) {
    if (dot) dot.className = 'status-dot working';
    if (snippetEl) snippetEl.innerText = `${runningCount}개 세션 실행 중`;
  } else {
    if (dot) dot.className = 'status-dot idle';
    if (snippetEl) snippetEl.innerText = '대기 중';
  }
}

function formatAgyResetTooltip(isoStr) {
  if (!isoStr) return '';
  try {
    const d = new Date(isoStr);
    if (isNaN(d.getTime())) return isoStr;
    const now = new Date();
    const diffMs = d.getTime() - now.getTime();
    const diffHours = Math.max(0, diffMs / (1000 * 60 * 60));
    const month = d.getMonth() + 1;
    const date = d.getDate();
    const hours = String(d.getHours()).padStart(2, '0');
    const mins = String(d.getMinutes()).padStart(2, '0');
    let relStr = '';
    if (diffMs <= 0) {
      relStr = '곧 리셋';
    } else if (diffHours < 24) {
      const diffMins = Math.round(diffMs / 60000);
      relStr = diffHours < 1 ? `약 ${diffMins}분 후` : `약 ${diffHours.toFixed(1)}시간 후`;
    } else {
      const days = Math.floor(diffHours / 24);
      const remH = Math.round(diffHours % 24);
      relStr = `약 ${days}일 ${remH}시간 후`;
    }
    return `${month}/${date} ${hours}:${mins} (${relStr})`;
  } catch (_) {
    return isoStr;
  }
}

function formatAgyRelativeTime(ts, cachedAtStr) {
  if (!ts && !cachedAtStr) return '확인 불가';
  let timeMs = 0;
  if (typeof ts === 'number') {
    timeMs = ts > 1e11 ? ts : ts * 1000;
  } else if (cachedAtStr) {
    timeMs = new Date(cachedAtStr).getTime();
  }
  if (!timeMs || isNaN(timeMs)) return cachedAtStr || '확인 불가';

  const now = Date.now();
  const diffSec = Math.max(0, Math.floor((now - timeMs) / 1000));

  let rel = '';
  if (diffSec < 5) {
    rel = '방금 전';
  } else if (diffSec < 60) {
    rel = `${diffSec}초 전`;
  } else if (diffSec < 3600) {
    const m = Math.floor(diffSec / 60);
    const s = diffSec % 60;
    rel = s > 0 ? `${m}분 ${s}초 전` : `${m}분 전`;
  } else {
    const h = Math.floor(diffSec / 3600);
    const m = Math.floor((diffSec % 3600) / 60);
    rel = m > 0 ? `${h}시간 ${m}분 전` : `${h}시간 전`;
  }

  const d = new Date(timeMs);
  const hours = String(d.getHours()).padStart(2, '0');
  const mins = String(d.getMinutes()).padStart(2, '0');
  const secs = String(d.getSeconds()).padStart(2, '0');
  const timeStr = `${hours}:${mins}:${secs}`;

  return `${rel} (${timeStr})`;
}

let lastAgyUsageData = null;

function buildAgyUsageTooltip(usage) {
  if (!usage) return 'AGY 쿼터 잔여량';
  const tooltipLines = ['[AGY 쿼터 잔여량]'];

  if (usage.five_hour && typeof usage.five_hour.pct === 'number') {
    const resetDesc = formatAgyResetTooltip(usage.five_hour.reset_time);
    tooltipLines.push(`• 5시간: ${usage.five_hour.pct}%${resetDesc ? ' (리셋: ' + resetDesc + ')' : ''}`);
  }

  if (usage.weekly && typeof usage.weekly.pct === 'number') {
    const resetDesc = formatAgyResetTooltip(usage.weekly.reset_time);
    tooltipLines.push(`• 주간: ${usage.weekly.pct}%${resetDesc ? ' (리셋: ' + resetDesc + ')' : ''}`);
  }

  const relTime = formatAgyRelativeTime(usage.timestamp, usage.cached_at);
  tooltipLines.push(`• 갱신: ${relTime}`);

  const source = usage.source || 'agy CLI (/usage 캐시)';
  tooltipLines.push(`• 출처: ${source}`);

  return tooltipLines.join('\n');
}

function getQuotaStatusClass(pct) {
  if (pct >= 50) return 'safe';
  if (pct >= 20) return 'warning';
  return 'danger';
}

function getQuotaStatusDot(cls) {
  if (cls === 'safe') return '🟢';
  if (cls === 'warning') return '🟡';
  return '🔴';
}

function getQuotaGroupsFromUsage(usage) {
  if (!usage) return [];
  if (Array.isArray(usage.groups) && usage.groups.length > 0) {
    return usage.groups;
  }
  if (usage.five_hour || usage.weekly) {
    return [{
      name: 'Gemini Models',
      five_hour: usage.five_hour || null,
      weekly: usage.weekly || null,
      buckets: [],
    }];
  }
  return [];
}

function renderQuotaBucketRowHtml(label, bucket) {
  if (!bucket || typeof bucket.pct !== 'number') return '';
  const rawPct = Math.max(0, Math.min(100, bucket.pct));
  const barPct = Math.round(rawPct);
  const pctText = Number.isInteger(rawPct) ? `${rawPct}%` : `${rawPct.toFixed(1)}%`;
  const cls = getQuotaStatusClass(rawPct);
  const dot = getQuotaStatusDot(cls);
  const resetDesc = bucket.reset_time ? formatAgyResetTooltip(bucket.reset_time) : '';
  return (
    `<div class="usage-bucket-row">` +
      `<div class="usage-bucket-top">` +
        `<span class="usage-bucket-label">${escapeHtml(label)}</span>` +
        `<span class="usage-bucket-pct ${cls}">${dot} ${pctText}</span>` +
      `</div>` +
      `<div class="usage-bar-track"><span class="usage-bar-fill ${cls}" style="width:${barPct}%"></span></div>` +
      (resetDesc ? `<div class="usage-bucket-reset">초기화: ${escapeHtml(resetDesc)}</div>` : '') +
    `</div>`
  );
}

function renderAllModelsUsageHtml(usage) {
  const groups = getQuotaGroupsFromUsage(usage);
  if (groups.length === 0) {
    return `<div class="setting-row"><div class="setting-desc">사용량 캐시 데이터가 없습니다. 새로고침을 눌러 조회하세요.</div></div>`;
  }

  const groupBlocks = groups.map(group => {
    const rows = [];
    if (group.weekly) rows.push(renderQuotaBucketRowHtml('주간 한도', group.weekly));
    if (group.five_hour) rows.push(renderQuotaBucketRowHtml('5시간 한도', group.five_hour));
    if (rows.length === 0 && Array.isArray(group.buckets)) {
      group.buckets.forEach(b => {
        rows.push(renderQuotaBucketRowHtml(b.name || b.window || '한도', b));
      });
    }
    if (rows.length === 0) return '';
    return (
      `<div class="usage-group-block">` +
        `<div class="usage-group-header">` +
          `<span class="usage-group-name">${escapeHtml(group.name || '모델 그룹')}</span>` +
        `</div>` +
        `<div class="usage-bucket-list">${rows.join('')}</div>` +
      `</div>`
    );
  }).filter(Boolean);

  if (groupBlocks.length === 0) {
    return `<div class="setting-row"><div class="setting-desc">표시할 모델 사용량 정보가 없습니다.</div></div>`;
  }

  const relTime = formatAgyRelativeTime(usage.timestamp, usage.cached_at);
  const source = usage.source || 'agy CLI (/usage 캐시)';
  const metaHtml =
    `<div class="usage-meta-row">` +
      `<span>마지막 갱신: ${escapeHtml(relTime)}</span>` +
      `<span>출처: ${escapeHtml(source)}</span>` +
    `</div>`;

  return groupBlocks.join('') + metaHtml;
}

function renderAllModelsUsageTargets(usage) {
  const html = renderAllModelsUsageHtml(usage);
  ['settings-usage-list', 'modal-usage-list'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.innerHTML = html;
  });
}

function openAllUsageModal() {
  renderAllModelsUsageTargets(lastAgyUsageData);
  openModal('modal-all-usage');
  if (!lastAgyUsageData) {
    fetchAgyUsage(false);
  }
}

function openUsageInSettings() {
  closeModal('modal-all-usage');
  switchView('settings');
  setTimeout(() => {
    const el = document.getElementById('settings-group-usage');
    if (el && typeof el.scrollIntoView === 'function') {
      el.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  }, 50);
}

async function refreshAllModelsUsage() {
  const btns = document.querySelectorAll('.js-usage-refresh-btn');
  btns.forEach(btn => {
    btn.disabled = true;
    btn.dataset.origText = btn.textContent;
    btn.textContent = '갱신 중...';
  });
  try {
    await fetchAgyUsage(true);
  } finally {
    btns.forEach(btn => {
      btn.disabled = false;
      btn.textContent = btn.dataset.origText || '새로고침';
    });
  }
}

function updateAvatarUsageBadge(usage) {
  lastAgyUsageData = usage || null;
  renderAllModelsUsageTargets(lastAgyUsageData);

  const badge = document.getElementById('avatar-usage-badge');
  if (!badge) return;
  if (!usage || (!usage.five_hour && !usage.weekly && !usage.badge_text)) {
    badge.style.display = 'none';
    return;
  }

  const items = [];

  // 5시간 세션 쿼터 (미니 슬림 바 + 퍼센트)
  // 자식 요소에 title 속성을 넣으면 상위 요소의 상세 툴팁이 가려지므로 상위 툴팁 단일화
  if (usage.five_hour && typeof usage.five_hour.pct === 'number') {
    const pct = Math.max(0, Math.min(100, Math.round(usage.five_hour.pct)));
    const cls = getQuotaStatusClass(pct);
    items.push(
      `<div class="quota-pill">` +
        `<span class="quota-tag">5h</span>` +
        `<span class="quota-track"><span class="quota-fill ${cls}" style="width:${pct}%"></span></span>` +
        `<span class="quota-num ${cls}">${pct}%</span>` +
      `</div>`
    );
  }

  // 주간 쿼터 (미니 슬림 바 + 퍼센트)
  if (usage.weekly && typeof usage.weekly.pct === 'number') {
    const pct = Math.max(0, Math.min(100, Math.round(usage.weekly.pct)));
    const cls = getQuotaStatusClass(pct);
    items.push(
      `<div class="quota-pill">` +
        `<span class="quota-tag">W</span>` +
        `<span class="quota-track"><span class="quota-fill ${cls}" style="width:${pct}%"></span></span>` +
        `<span class="quota-num ${cls}">${pct}%</span>` +
      `</div>`
    );
  }

  // 데이터 구조상 텍스트만 있는 경우 폴백
  if (items.length === 0 && usage.badge_text) {
    items.push(`<span class="quota-num">${escapeHtml(usage.badge_text)}</span>`);
  }

  if (items.length === 0) {
    badge.style.display = 'none';
    return;
  }

  badge.innerHTML = items.join('');
  badge.style.display = 'inline-flex';
  badge.title = buildAgyUsageTooltip(usage);

  // 마우스 호버 시 실시간 상대 시간 재계산 (1회 등록)
  if (!badge.dataset.hoverBound) {
    badge.dataset.hoverBound = 'true';
    badge.addEventListener('mouseenter', () => {
      if (lastAgyUsageData) {
        badge.title = buildAgyUsageTooltip(lastAgyUsageData);
      }
    });
  }
}

let usagePollingTimer = null;
const USAGE_POLL_INTERVAL_MS = 45000; // 45초 주기 (30~60초 권고)

async function fetchAgyUsage(forceRefresh = false) {
  if (document.hidden) return; // 비활성 탭 절전
  try {
    const url = forceRefresh ? '/api/usage?refresh=1' : '/api/usage';
    const res = await fetch(url);
    if (!res.ok) return;
    const data = await res.json();
    if (data && data.usage) {
      updateAvatarUsageBadge(data.usage);
    }
  } catch (_) {}
}

let usagePollingInitialized = false;
function initUsagePolling() {
  if (usagePollingInitialized) return;
  usagePollingInitialized = true;

  fetchAgyUsage(false);

  if (usagePollingTimer) clearInterval(usagePollingTimer);
  usagePollingTimer = setInterval(() => {
    if (!document.hidden) {
      fetchAgyUsage(false);
    }
  }, USAGE_POLL_INTERVAL_MS);

  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
      fetchAgyUsage(false);
    }
  });
}

function showToast(msg) {
  const toast = document.getElementById('toast');
  if (!toast) return;
  toast.innerText = msg;
  toast.classList.add('show');
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove('show'), 2400);
}

function copyToClipboard(text, label, raw = false) {
  if (!text || text === '조회 중...') return;
  const cleanText = raw
    ? String(text).trim()
    : String(text).replace(/\s*\(.*?\)\s*/g, '').trim();
  if (!cleanText) return;
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(cleanText).then(() => {
      showToast((label ? label + ' ' : '') + '복사됨');
    }).catch(() => fallbackCopyText(cleanText, label));
  } else {
    fallbackCopyText(cleanText, label);
  }
}

function copyFileLinkPath(btn) {
  const path = btn && btn.getAttribute('data-path');
  if (path) copyToClipboard(path, '파일 경로가', true);
}

function fallbackCopyText(text, label) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.left = '-9999px';
  document.body.appendChild(ta);
  ta.select();
  try {
    document.execCommand('copy');
    showToast((label ? label + ' ' : '') + '복사됨');
  } catch (_) {
    showToast('복사에 실패했습니다.');
  }
  document.body.removeChild(ta);
}
