// ─── 대화방 스토리지 & 서버 동기화 (SQLite chat.db 단일 SSOT) ───
let isInitialRoomsLoading = true;

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
  const ensureAgent = (id, title, desc, workdir, extra = {}) => {
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
        model: extra.model || null,
        effort: extra.effort || null,
        parentAgentId: extra.parentAgentId || null,
        updatedAt: 0
      };
      chatRooms.push(room);
    } else {
      room.title = title;
      if (desc) room.preview = room.preview || desc;
      if (workdir) {
        room.workdir = workdir;
      } else if (!room.workdir && serverStatus) {
        room.workdir = serverStatus.vaultDir || serverStatus.workdir || '';
      }
      if (extra.model && !room.model) room.model = extra.model;
      if (extra.effort && !room.effort) room.effort = extra.effort;
    }
    return room;
  };

  const defaultVaultDir = (serverStatus && (serverStatus.vaultDir || serverStatus.workdir)) || '';
  const defaultCodeDir = (serverStatus && (serverStatus.codeDir || serverStatus.codedir || serverStatus.vaultDir)) || '';

  const coreCfg = getAgentConfigById(CORE_ROOM_ID);
  const generalCfg = getAgentConfigById(EXPERT_GENERAL_ROOM_ID);
  const vaultCfg = getAgentConfigById(EXPERT_VAULT_ROOM_ID);
  const codingCfg = getAgentConfigById(EXPERT_CODING_ROOM_ID);

  const coreRoom = ensureAgent(
    CORE_ROOM_ID,
    (coreCfg && coreCfg.name) || CORE_ROOM_TITLE,
    (coreCfg && coreCfg.roleDescription) || '전체 AGY 세션 열람 · 지시 · 스폰',
    (coreCfg && coreCfg.workdir) || defaultVaultDir,
    coreCfg || {}
  );
  ensureAgent(
    EXPERT_GENERAL_ROOM_ID,
    (generalCfg && generalCfg.name) || EXPERT_GENERAL_TITLE,
    (generalCfg && generalCfg.roleDescription) || '사전 제약 없이 자유롭게 대화하는 순수 범용 비서',
    (generalCfg && generalCfg.workdir) || defaultVaultDir,
    generalCfg || {}
  );
  ensureAgent(
    EXPERT_VAULT_ROOM_ID,
    (vaultCfg && vaultCfg.name) || EXPERT_VAULT_TITLE,
    (vaultCfg && vaultCfg.roleDescription) || '옵시디언 볼트 지식 · 개인 기록 전담',
    (vaultCfg && vaultCfg.workdir) || defaultVaultDir,
    vaultCfg || {}
  );
  ensureAgent(
    EXPERT_CODING_ROOM_ID,
    (codingCfg && codingCfg.name) || EXPERT_CODING_TITLE,
    (codingCfg && codingCfg.roleDescription) || 'oshyun-bot 개발 · 테스트 전담',
    (codingCfg && codingCfg.workdir) || defaultCodeDir,
    codingCfg || {}
  );

  if (Array.isArray(agentConfigs)) {
    for (const cfg of agentConfigs) {
      if (!cfg || !cfg.id) continue;
      if (!ORCHESTRATOR_ROOM_IDS.includes(cfg.id)) {
        ORCHESTRATOR_ROOM_IDS.push(cfg.id);
      }
      if ([CORE_ROOM_ID, EXPERT_GENERAL_ROOM_ID, EXPERT_VAULT_ROOM_ID, EXPERT_CODING_ROOM_ID].includes(cfg.id)) continue;
      ensureAgent(
        cfg.id,
        cfg.name || cfg.id,
        cfg.roleDescription || '사용자 정의 전문 에이전트',
        cfg.workdir || defaultVaultDir,
        cfg
      );
    }
  }
  return coreRoom;
}

function updateCoreAgentSlotUi() {
  ensureCoreRoom();
  const updateSlot = (slotId, subId, roomId, titleId = null) => {
    const slot = document.getElementById(slotId);
    const subEl = document.getElementById(subId);
    if (!slot) return;
    const cfg = getAgentConfigById(roomId);
    if (cfg && titleId) {
      const tEl = document.getElementById(titleId);
      if (tEl && cfg.name) tEl.textContent = cfg.name;
    }
    if (cfg && cfg.roleDescription) {
      slot.title = cfg.roleDescription;
    }
    const live = roomLiveStates[roomId];
    const isRunning = Boolean(live && live.isRunning);
    slot.classList.toggle('active', currentRoomId === roomId && currentView === 'chat');
    slot.classList.toggle('running', isRunning);
    if (subEl) {
      subEl.textContent = isRunning ? '작업 중' : '대기';
    }
  };

  updateSlot('core-agent-slot', 'core-agent-sub', CORE_ROOM_ID, 'core-agent-title');

  // 전체 에이전트 목록을 바탕으로 parentAgentId 계층 트리 구성
  let allAgents = Array.isArray(agentConfigs) && agentConfigs.length > 0
    ? agentConfigs.filter(a => a && a.id && a.id !== CORE_ROOM_ID)
    : [];

  const defaultAgents = [
    { id: EXPERT_GENERAL_ROOM_ID, name: '일반 대화', roleDescription: '사전 제약 없이 자유롭게 대화하는 순수 범용 비서', emoji: '💬', color: '#64748b', parentAgentId: CORE_ROOM_ID, builtin: true },
    { id: EXPERT_VAULT_ROOM_ID, name: '세컨브레인', roleDescription: 'Obsidian 볼트 지식·5대 도메인 동기화 전문 에이전트', emoji: '🧠', color: '#d97706', parentAgentId: CORE_ROOM_ID, builtin: true },
    { id: EXPERT_CODING_ROOM_ID, name: '봇개발', roleDescription: 'oshyun-bot 소스코드 개발·테스트·토큰버닝 전문 에이전트', emoji: '💻', color: '#3b82f6', parentAgentId: CORE_ROOM_ID, builtin: true },
  ];
  for (const def of defaultAgents) {
    if (!allAgents.some(a => a && a.id === def.id)) {
      allAgents.push(def);
    }
  }

  const agentIdSet = new Set(allAgents.map(a => a.id));
  const childrenByParent = new Map();
  for (const a of allAgents) {
    let p = String(a.parentAgentId || '').trim();
    if (!p || p === CORE_ROOM_ID || !agentIdSet.has(p) || p === a.id) {
      p = CORE_ROOM_ID;
    }
    if (!childrenByParent.has(p)) childrenByParent.set(p, []);
    childrenByParent.get(p).push(a);
  }

  // 재귀적 계층 트리 렌더링 함수
  const renderAgentBranch = (parentId) => {
    const children = childrenByParent.get(parentId) || [];
    if (children.length === 0) return '';

    return children.map(agent => {
      const safeId = escapeHtml(agent.id);
      const live = roomLiveStates[agent.id];
      const isRunning = Boolean(live && live.isRunning);
      const isActive = (currentRoomId === agent.id && currentView === 'chat');
      const name = escapeHtml(agent.name || agent.id);
      const desc = escapeHtml(agent.roleDescription || '전문 에이전트');

      const isGeneral = (agent.id === EXPERT_GENERAL_ROOM_ID);
      const isVault = (agent.id === EXPERT_VAULT_ROOM_ID);
      const isCoding = (agent.id === EXPERT_CODING_ROOM_ID);

      const slotId = isGeneral ? 'expert-general-slot' : (isVault ? 'expert-vault-slot' : (isCoding ? 'expert-coding-slot' : `custom-slot-${safeId}`));
      const titleId = isGeneral ? 'expert-general-title' : (isVault ? 'expert-vault-title' : (isCoding ? 'expert-coding-title' : ''));
      const subId = isGeneral ? 'expert-general-sub' : (isVault ? 'expert-vault-sub' : (isCoding ? 'expert-coding-sub' : `custom-sub-${safeId}`));
      const cfgBtnId = isGeneral ? 'btn-expert-general-cfg' : (isVault ? 'btn-expert-vault-cfg' : (isCoding ? 'btn-expert-coding-cfg' : `btn-agent-cfg-${safeId}`));
      const resetBtnId = isGeneral ? 'btn-expert-general-reset' : (isVault ? 'btn-expert-vault-reset' : (isCoding ? 'btn-expert-coding-reset' : `btn-agent-reset-${safeId}`));
      const treeId = isGeneral ? 'expert-general-subtask-tree' : (isVault ? 'expert-vault-subtask-tree' : (isCoding ? 'expert-coding-subtask-tree' : `custom-subtask-tree-${safeId}`));

      const colorStyle = agent.color
        ? `background:${escapeHtml(agent.color)}26;color:${escapeHtml(agent.color)};`
        : (isGeneral ? 'background:rgba(100,116,139,0.15);color:#64748b;' : (isVault ? 'background:rgba(217,119,6,0.15);color:#d97706;' : (isCoding ? 'background:rgba(59,130,246,0.15);color:#3b82f6;' : 'background:rgba(16,185,129,0.15);color:#10b981;')));

      let iconHtml;
      if (isGeneral) {
        iconHtml = `<svg class="i" viewBox="0 0 24 24"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>`;
      } else if (isVault) {
        iconHtml = `<svg class="i" viewBox="0 0 24 24"><path d="M9.5 2A2.5 2.5 0 0 1 12 4.5v15a2.5 2.5 0 0 1-4.96.44 2.5 2.5 0 0 1-2.96-3.08 3 3 0 0 1-.34-5.58 2.5 2.5 0 0 1 1.32-4.24 2.5 2.5 0 0 1 4.44-2.04"/><path d="M14.5 2A2.5 2.5 0 0 0 12 4.5v15a2.5 2.5 0 0 0 4.96.44 2.5 2.5 0 0 0 2.96-3.08 3 3 0 0 0 .34-5.58 2.5 2.5 0 0 0-1.32-4.24 2.5 2.5 0 0 0-4.44-2.04"/></svg>`;
      } else if (isCoding) {
        iconHtml = `<svg class="i" viewBox="0 0 24 24"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>`;
      } else if (!agent.emoji || agent.emoji === '🤖') {
        iconHtml = getAgentMonolineIconSvg(agent.id);
      } else {
        iconHtml = `<span class="agent-cfg-symbol">${escapeHtml(agent.emoji)}</span>`;
      }

      // 하위 에이전트 재귀 중첩 렌더링
      const subAgentsHtml = renderAgentBranch(agent.id);

      return `
        <div class="expert-agent-node" id="expert-node-${safeId}">
          <div class="sb-expert-row">
            <div class="core-agent-slot expert-slot ${isActive ? 'active' : ''} ${isRunning ? 'running' : ''}" id="${slotId}" onclick="selectRoom('${safeId}')" title="${desc}">
              <div class="core-agent-icon" style="${colorStyle}">
                ${iconHtml}
              </div>
              <div class="core-agent-body">
                <div class="core-agent-top">
                  <span class="core-agent-title" ${titleId ? `id="${titleId}"` : ''}>${name}</span>
                </div>
                <div class="core-agent-sub" id="${subId}">${isRunning ? '작업 중' : '대기'}</div>
              </div>
            </div>
            <button type="button" class="core-agent-cfg-btn expert-cfg-btn" id="${cfgBtnId}" onclick="openAgentConfigModal('${safeId}', event)" title="${name} 에이전트 지침 및 설정">
              <svg class="i" viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
            </button>
            <button type="button" class="core-agent-reset-btn expert-reset-btn" id="${resetBtnId}" onclick="resetCoreOrchestratorRoom(event, '${safeId}')" title="${name} 에이전트 세션 초기화">
              <svg class="i" viewBox="0 0 24 24"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>
            </button>
          </div>
          <!-- 📂 하위 브랜치 (직속 채팅 + 산하 에이전트 통합) -->
          <div class="sb-agent-branch" id="branch-${safeId}">
            <!-- 직속 하위 채팅 트리 -->
            <div class="sb-core-tree sb-subtask-tree" id="${treeId}"></div>
            <!-- 산하 하위 에이전트 브랜치 (중첩 트리) -->
            <div class="sb-subagents-container" id="subagents-${safeId}">${subAgentsHtml}</div>
          </div>
        </div>
      `;
    }).join('');
  };

  const treeContainer = document.getElementById('sb-agent-tree-container');
  if (treeContainer) {
    treeContainer.innerHTML = renderAgentBranch(CORE_ROOM_ID);
  } else {
    // 폴백: custom-agents-branch가 있는 구버전 DOM 지원
    const customBranch = document.getElementById('custom-agents-branch');
    if (customBranch) {
      customBranch.innerHTML = renderAgentBranch(CORE_ROOM_ID);
    }
  }

  // 폴백용 custom-agents-branch 클리어
  const legacyBranch = document.getElementById('custom-agents-branch');
  if (legacyBranch && treeContainer) {
    legacyBranch.innerHTML = '';
  }
}

function getOrchestratorRoom(roomId) {
  ensureCoreRoom();
  const rid = roomId || CORE_ROOM_ID;
  return chatRooms.find(r => r && r.id === rid) || ensureCoreRoom();
}

function applyOrchestratorSessionResetLocal(targetRoomId, data = {}) {
  const rid = targetRoomId || CORE_ROOM_ID;
  const room = getOrchestratorRoom(rid);
  const cfg = getAgentConfigById(rid);
  if (room) {
    room.messages = [];
    room.messageCount = 0;
    if (cfg && cfg.roleDescription) {
      room.preview = cfg.roleDescription;
    } else if (rid === EXPERT_GENERAL_ROOM_ID) {
      room.preview = '사전 제약 없이 자유롭게 대화하는 순수 범용 비서';
    } else if (rid === EXPERT_VAULT_ROOM_ID) {
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
  const isGeneral = targetId === EXPERT_GENERAL_ROOM_ID;
  const isVault = targetId === EXPERT_VAULT_ROOM_ID;
  const isCoding = targetId === EXPERT_CODING_ROOM_ID;
  const cfg = getAgentConfigById(targetId);
  let agentScope = 'master';
  if (isGeneral) agentScope = 'general';
  else if (isVault) agentScope = 'vault';
  else if (isCoding) agentScope = 'coding';
  else if (targetId !== CORE_ROOM_ID) agentScope = targetId;
  const agentLabel = (cfg && cfg.name) || (isGeneral ? '일반 대화' : (isVault ? '세컨브레인' : (isCoding ? '봇개발' : '마스터')));

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
    const fetchFn = typeof fetchWithTimeout === 'function' ? fetchWithTimeout : fetch;
    const res = await fetchFn('/api/chat/rooms', {}, 2500);
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
        else if (canonId === EXPERT_GENERAL_ROOM_ID) title = EXPERT_GENERAL_TITLE;
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

      // 현재 활성화된 방은 동기화 후에도 unread 가 부활하지 않도록 낙관적 상태 유지
      if (currentRoomId) {
        const curRoomObj = getRoomById(currentRoomId);
        if (curRoomObj && curRoomObj.unread) {
          curRoomObj.unread = false;
        }
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
    } else {
      throw new Error((data && data.error) || '대화방 목록 응답 형식 비정상');
    }
  } catch (err) {
    console.warn('[RoomsSync] 대화방 목록 로드 실패, 안전망 폴백 가동:', err);
    ensureCoreRoom();
    if (!chatRooms.some(r => r && !ORCHESTRATOR_ROOM_IDS.includes(r.id))) {
      const defaultId = 'room-' + Date.now();
      chatRooms.push({
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
    if (!currentRoomId || !chatRooms.some(r => r && (r.id === currentRoomId || r.sessionId === currentRoomId))) {
      currentRoomId = chatRooms[0].id;
    }
    renderRoomList();
    updateSessionBar({ skipMasterUi: true });
    syncMasterModeUi({ loadData: false });
    const cur = getCurrentRoom();
    if (cur) cur.historyLoaded = true;
    renderMessagesForCurrentRoom(true);
    focusChatInput();
  } finally {
    isInitialRoomsLoading = false;
    if (typeof markAppReady === 'function') {
      markAppReady();
    }
  }
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
    const fetchFn = typeof fetchWithTimeout === 'function' ? fetchWithTimeout : fetch;
    const res = await fetchFn(url, {}, 3000);
    const data = await res.json();
    if (_historyFetchSeq[targetId] !== reqSeq) return;
    let room = getRoomById(roomId) || getRoomById(sessionId);
    if (data.ok && Array.isArray(data.messages)) {
      if (!room && roomId) {
        room = {
          id: roomId,
          roomId: roomId,
          title: data.title || '하위 세션',
          preview: '',
          messageCount: data.messages.length,
          messages: [],
          updatedAt: Date.now()
        };
        chatRooms.push(room);
      }
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
        if (typeof currentThreadRoomId !== 'undefined' && currentThreadRoomId && (room.id === currentThreadRoomId || room.sessionId === currentThreadRoomId)) {
          if (typeof renderThreadPanelBody === 'function') {
            renderThreadPanelBody();
          }
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
    if (room) {
      room.historyLoaded = true;
    }
    if (room && room.id === currentRoomId) {
      renderMessagesForCurrentRoom(true);
    }
    if (typeof currentThreadRoomId !== 'undefined' && currentThreadRoomId && (roomId === currentThreadRoomId || sessionId === currentThreadRoomId)) {
      if (typeof renderThreadPanelBody === 'function') {
        renderThreadPanelBody();
      }
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
  if (typeof updatePwaManifestForRoom === 'function') {
    updatePwaManifestForRoom(room.id);
  }

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

  // 사이드바(데스크톱) + 상단바(모바일: 사이드바는 오프캔버스 드로어라 안 보임) 배지 모두 갱신
  const badges = document.querySelectorAll('.avatar-usage-badge');
  if (!badges.length) return;
  if (!usage || (!usage.five_hour && !usage.weekly && !usage.badge_text)) {
    badges.forEach(b => { b.style.display = 'none'; });
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
    badges.forEach(b => { b.style.display = 'none'; });
    return;
  }

  const html = items.join('');
  const tip = buildAgyUsageTooltip(usage);
  badges.forEach(badge => {
    badge.innerHTML = html;
    badge.style.display = 'inline-flex';
    badge.title = tip;
    // 마우스 호버 시 실시간 상대 시간 재계산 (1회 등록)
    if (!badge.dataset.hoverBound) {
      badge.dataset.hoverBound = 'true';
      badge.addEventListener('mouseenter', () => {
        if (lastAgyUsageData) badge.title = buildAgyUsageTooltip(lastAgyUsageData);
      });
    }
  });
}

let usagePollingTimer = null;
const USAGE_POLL_INTERVAL_MS = 45000; // 45초 주기 (30~60초 권고)

async function fetchAgyUsage(forceRefresh = false) {
  // 절전: 주기적 자동 폴링이고 문서가 비활성인 경우에만 스킵
  // 강제 갱신(forceRefresh)이거나 최초 로드 시에는 안드로이드 PWA 시작 시점의 hidden 여부와 무관하게 조회 진행
  if (!forceRefresh && document.hidden) return;
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

  // 최초 로드 시 forceRefresh=true로 즉시 조회하여 PWA standalone 화면에서도 대기 없이 배지 표시
  fetchAgyUsage(true);

  if (usagePollingTimer) clearInterval(usagePollingTimer);
  usagePollingTimer = setInterval(() => {
    if (!document.hidden) {
      fetchAgyUsage(false);
    }
  }, USAGE_POLL_INTERVAL_MS);

  // 안드로이드 PWA(standalone) 및 모바일 브라우저 라이프사이클 대응:
  // 1) 탭/앱 전환 및 화면 켜짐 (visibilitychange)
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
      fetchAgyUsage(false);
    }
  });

  // 2) PWA 백그라운드 복귀 및 창 포커스 복원 (focus, pageshow)
  window.addEventListener('focus', () => {
    fetchAgyUsage(false);
  });
  window.addEventListener('pageshow', () => {
    fetchAgyUsage(false);
  });
}

if (typeof window.openRoom !== 'function') {
  window.openRoom = function(roomId, options) {
    if (typeof selectRoom === 'function') {
      return selectRoom(roomId, options);
    }
  };
}

function hideToast() {
  const toast = document.getElementById('toast');
  if (!toast) return;
  toast.classList.remove('show');
  toast.classList.remove('clickable');
  if (toastTimer) {
    clearTimeout(toastTimer);
    toastTimer = null;
  }
}

function findRoomIdByTaskId(taskId) {
  if (!taskId) return null;
  const cleanId = String(taskId).trim();
  if (typeof latestMasterTasks !== 'undefined' && Array.isArray(latestMasterTasks)) {
    const task = latestMasterTasks.find(t => t && (t.id === cleanId || t.taskId === cleanId));
    if (task && task.roomId) return task.roomId;
  }
  if (typeof chatRooms !== 'undefined' && Array.isArray(chatRooms)) {
    const room = chatRooms.find(r => r && (r.id === cleanId || r.taskId === cleanId));
    if (room && room.id) return room.id;
  }
  return null;
}

function showToast(msg, options = {}) {
  const toast = document.getElementById('toast');
  if (!toast) return;

  const opts = (typeof options === 'string') ? { type: options } : (options || {});
  let targetRoomId = opts.roomId || null;
  if (!targetRoomId && opts.taskId) {
    targetRoomId = findRoomIdByTaskId(opts.taskId);
  }

  const textMsg = String(msg || '').trim();
  if (!textMsg) return;

  toast.innerHTML = '';

  const contentEl = document.createElement('div');
  contentEl.className = 'toast-content';

  const msgEl = document.createElement('span');
  msgEl.className = 'toast-message';
  msgEl.textContent = textMsg;
  contentEl.appendChild(msgEl);

  const hasTargetRoom = Boolean(targetRoomId);

  if (hasTargetRoom) {
    const actionBadge = document.createElement('span');
    actionBadge.className = 'toast-action-badge';
    actionBadge.textContent = '이동 →';
    contentEl.appendChild(actionBadge);
    toast.classList.add('clickable');
    toast.title = '클릭하여 해당 대화방으로 이동';
  } else {
    toast.classList.remove('clickable');
    toast.removeAttribute('title');
  }

  toast.appendChild(contentEl);

  // 닫기(X) 버튼 추가 — 방 이동 없이 토스트만 닫히도록 이벤트 버블링 분리(stopPropagation)
  const closeBtn = document.createElement('button');
  closeBtn.type = 'button';
  closeBtn.className = 'toast-close-btn';
  closeBtn.setAttribute('aria-label', '닫기');
  closeBtn.innerHTML = '&times;';
  closeBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    hideToast();
  });
  toast.appendChild(closeBtn);

  // 토스트 본체 클릭 이벤트 — 해당 채팅방으로 즉시 화면 전환
  toast.onclick = (e) => {
    if (hasTargetRoom) {
      if (typeof selectRoom === 'function') {
        selectRoom(targetRoomId);
      } else if (typeof openRoom === 'function') {
        openRoom(targetRoomId);
      }
      hideToast();
    } else if (typeof opts.onClick === 'function') {
      opts.onClick(e);
      hideToast();
    }
  };

  toast.classList.add('show');

  const duration = typeof opts.duration === 'number' ? opts.duration : (hasTargetRoom ? 4500 : 2800);
  if (toastTimer) clearTimeout(toastTimer);

  let remainingMs = duration;
  let timerStart = Date.now();

  const startTimer = (ms) => {
    toastTimer = setTimeout(() => {
      hideToast();
    }, ms);
    timerStart = Date.now();
    remainingMs = ms;
  };

  startTimer(duration);

  // 마우스 호버 시 사라지지 않도록 일시정지, 마우스 나갈 때 재개
  toast.onmouseenter = () => {
    if (toastTimer) {
      clearTimeout(toastTimer);
      toastTimer = null;
      remainingMs -= (Date.now() - timerStart);
      if (remainingMs < 1000) remainingMs = 1000;
    }
  };

  toast.onmouseleave = () => {
    if (toast.classList.contains('show')) {
      startTimer(remainingMs);
    }
  };
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

// ─── 에이전트 레벨 설정 & 하위 채팅 프롬프트 투명 관리 / 신규 커스텀 에이전트 생성 ───
let currentAgentConfigTab = 'edit'; // 'edit' | 'preview'
let agentPromptPreviewTimer = null;

function syncAgentConfigsState(agentsList, orchestratorIds = null) {
  if (Array.isArray(agentsList)) {
    agentConfigs = agentsList.filter(a => a && typeof a === 'object' && a.id);
  }
  const nextOrchIds = new Set([CORE_ROOM_ID, EXPERT_GENERAL_ROOM_ID, EXPERT_VAULT_ROOM_ID, EXPERT_CODING_ROOM_ID]);
  if (Array.isArray(orchestratorIds)) {
    orchestratorIds.forEach(id => {
      if (id) nextOrchIds.add(String(id).trim());
    });
  }
  if (Array.isArray(agentConfigs)) {
    agentConfigs.forEach(a => {
      if (a && a.id) nextOrchIds.add(String(a.id).trim());
    });
  }
  ORCHESTRATOR_ROOM_IDS = Array.from(nextOrchIds);

  ensureCoreRoom();
  updateCoreAgentSlotUi();
  if (typeof renderMsbMemoryAgentTabs === 'function') {
    renderMsbMemoryAgentTabs();
  }
  if (typeof renderRoomList === 'function') {
    renderRoomList();
  }
  if (typeof updateSessionBar === 'function') {
    updateSessionBar({ skipMasterUi: true });
  }
}

async function loadAgentConfigsFromServer() {
  try {
    const fetchFn = typeof fetchWithTimeout === 'function' ? fetchWithTimeout : fetch;
    const res = await fetchFn('/api/orchestrator/agents', {}, 2500);
    const data = await res.json();
    if (data && data.ok && Array.isArray(data.agents)) {
      syncAgentConfigsState(data.agents, data.orchestratorRoomIds);
    }
  } catch (err) {
    console.warn('에이전트 설정 목록 로드 실패:', err);
  }
}

function getModelOptionsHtmlForAgentConfig(selectedModel) {
  const serverModel = (serverStatus && serverStatus.model) || 'gemini-3.8-flash';
  const curVal = selectedModel || serverModel;
  let modelsList = [];
  if (serverStatus && Array.isArray(serverStatus.availableModels) && serverStatus.availableModels.length > 0) {
    modelsList = serverStatus.availableModels;
  } else {
    modelsList = [
      { id: 'gemini-3.8-flash', label: 'Gemini 3.8 Flash' },
      { id: 'gemini-4-argon', label: 'Gemini 4 Argon' }
    ];
  }
  let html = '';
  let found = false;
  for (const m of modelsList) {
    const isSel = m.id === curVal;
    if (isSel) found = true;
    html += `<option value="${escapeHtml(m.id)}" ${isSel ? 'selected' : ''}>${escapeHtml(m.label || m.id)}</option>`;
  }
  if (curVal && !found) {
    html += `<option value="${escapeHtml(curVal)}" selected>${escapeHtml(curVal)}</option>`;
  }
  return html;
}

function getAgentMonolineIconSvg(agentId, className = 'i') {
  if (agentId === CORE_ROOM_ID || agentId === 'master') {
    return `<svg class="${className}" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="3"/><line x1="12" y1="3" x2="12" y2="6"/><line x1="12" y1="18" x2="12" y2="21"/><line x1="3" y1="12" x2="6" y2="12"/><line x1="18" y1="12" x2="21" y2="12"/></svg>`;
  }
  if (agentId === EXPERT_VAULT_ROOM_ID || agentId === 'second_brain') {
    return `<svg class="${className}" viewBox="0 0 24 24"><path d="M9.5 2A2.5 2.5 0 0 1 12 4.5v15a2.5 2.5 0 0 1-4.96.44 2.5 2.5 0 0 1-2.96-3.08 3 3 0 0 1-.34-5.58 2.5 2.5 0 0 1 1.32-4.24 2.5 2.5 0 0 1 4.44-2.04"/><path d="M14.5 2A2.5 2.5 0 0 0 12 4.5v15a2.5 2.5 0 0 0 4.96.44 2.5 2.5 0 0 0 2.96-3.08 3 3 0 0 0 .34-5.58 2.5 2.5 0 0 0-1.32-4.24 2.5 2.5 0 0 0-4.44-2.04"/></svg>`;
  }
  if (agentId === EXPERT_CODING_ROOM_ID || agentId === 'bot_dev') {
    return `<svg class="${className}" viewBox="0 0 24 24"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>`;
  }
  return `<svg class="${className}" viewBox="0 0 24 24"><rect x="3" y="11" width="18" height="10" rx="2"/><circle cx="12" cy="5" r="2"/><path d="M12 7v4"/><line x1="8" y1="16" x2="8" y2="16"/><line x1="16" y1="16" x2="16" y2="16"/></svg>`;
}

function getDescendantAgentIds(targetId) {
  const descendants = new Set();
  const queue = [targetId];
  while (queue.length > 0) {
    const cur = queue.shift();
    for (const a of (agentConfigs || [])) {
      if (a && a.id && a.parentAgentId === cur && !descendants.has(a.id)) {
        descendants.add(a.id);
        queue.push(a.id);
      }
    }
  }
  return descendants;
}

function renderAgentConfigSelectorBar(activeId) {
  const bar = document.getElementById('agent-cfg-selector-bar');
  if (!bar) return;
  const list = Array.isArray(agentConfigs) && agentConfigs.length > 0
    ? agentConfigs
    : [
        { id: CORE_ROOM_ID, name: '마스터 오케스트레이터', builtin: true },
        { id: EXPERT_GENERAL_ROOM_ID, name: '일반 대화', builtin: true },
        { id: EXPERT_VAULT_ROOM_ID, name: '세컨브레인', builtin: true },
        { id: EXPERT_CODING_ROOM_ID, name: '봇개발', builtin: true },
      ];

  const chipsHtml = list.map(a => {
    const isSel = (activeId && a.id === activeId);
    const safeId = escapeHtml(a.id);
    const name = escapeHtml(a.name || a.id);
    const iconHtml = (a.builtin || !a.emoji || a.emoji === '🤖' || ['👑', '🧠', '💻'].includes(a.emoji))
      ? getAgentMonolineIconSvg(a.id)
      : `<span class="agent-cfg-symbol">${escapeHtml(a.emoji)}</span>`;
    return `
      <button type="button" class="agent-cfg-chip ${isSel ? 'active' : ''}" onclick="openAgentConfigModal('${safeId}')">
        <span class="agent-cfg-chip-icon">${iconHtml}</span>
        <span>${name}</span>
      </button>
    `;
  }).join('');

  const isNewMode = !activeId;
  const addBtnHtml = `
    <button type="button" class="agent-cfg-chip is-new ${isNewMode ? 'active' : ''}" onclick="openAgentConfigModal(null)">
      <svg class="i" viewBox="0 0 24 24"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
      <span>새 에이전트 추가</span>
    </button>
  `;
  bar.innerHTML = chipsHtml + addBtnHtml;
}

function populateAgentConfigForm(targetAgentId = null) {
  editingAgentId = targetAgentId ? String(targetAgentId).trim() : null;
  const cfg = editingAgentId ? getAgentConfigById(editingAgentId) : null;
  const isBuiltin = Boolean(cfg && cfg.builtin) || [CORE_ROOM_ID, EXPERT_GENERAL_ROOM_ID, EXPERT_VAULT_ROOM_ID, EXPERT_CODING_ROOM_ID].includes(editingAgentId);
  const isCustomExisting = Boolean(editingAgentId && !isBuiltin && cfg);

  renderAgentConfigSelectorBar(editingAgentId);

  const avatarEl = document.getElementById('agent-cfg-modal-avatar');
  const titleEl = document.getElementById('agent-cfg-modal-title');
  const subEl = document.getElementById('agent-cfg-modal-subtitle');

  const defaultVaultDir = (serverStatus && (serverStatus.vaultDir || serverStatus.workdir)) || '';

  const formVals = cfg ? {
    id: cfg.id,
    name: cfg.name || '',
    emoji: cfg.emoji || '',
    parentAgentId: cfg.parentAgentId || CORE_ROOM_ID,
    roleDescription: cfg.roleDescription || '',
    workdir: cfg.workdir || defaultVaultDir,
    model: cfg.model || 'gemini-3.8-flash',
    effort: cfg.effort || 'medium',
    childModel: cfg.childModel || cfg.model || 'gemini-3.8-flash',
    childEffort: cfg.childEffort || cfg.effort || 'medium',
    injectChildTemplate: cfg.injectChildTemplate !== false,
    childPromptTemplate: cfg.childPromptTemplate || '',
    systemPrompt: cfg.systemPrompt || '',
  } : {
    id: '',
    name: '',
    emoji: '',
    parentAgentId: CORE_ROOM_ID,
    roleDescription: '',
    workdir: defaultVaultDir,
    model: 'gemini-3.8-flash',
    effort: 'medium',
    childModel: 'gemini-3.8-flash',
    childEffort: 'medium',
    injectChildTemplate: true,
    childPromptTemplate: `[하위 워커 동작 규칙]\n1. 지정된 작업 디렉토리 내에서 요청된 과업을 정확하고 신속하게 수행하라.\n2. 결론과 핵심 수치를 첫 줄에 두괄식으로 요약하여 보고하라.`,
    systemPrompt: `# 전문 에이전트 지침\n\n너는 oshyun-bot의 전문 에이전트다.\n\n## 행동 원칙\n1. 사용자의 실무 과업 및 위임 지시(~시켜, ~해줘, ~작성해봐, 위임해, 스폰해 등) 접수 시 즉시 하위 세션(session-ctl spawn)으로 비동기 위임하라.\n2. 사전 탐색(파일 열람/검색)으로 세션을 블로킹하지 말고 도구 호출 단 1회로 즉시 스폰한 뒤 착수 보고 후 턴을 종료하라.\n3. 단순 인사나 상태 확인에 한해서만 1초 내에 직접 응답하라.\n4. 사용자에게 항상 명확하고 간결하게 핵심 결론부터 한국어로 보고한다.`,
  };

  if (avatarEl) {
    if (!editingAgentId) {
      avatarEl.innerHTML = `<svg class="i" viewBox="0 0 24 24"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>`;
    } else if (isBuiltin || !formVals.emoji || formVals.emoji === '🤖' || ['👑', '🧠', '💻'].includes(formVals.emoji)) {
      avatarEl.innerHTML = getAgentMonolineIconSvg(editingAgentId);
    } else {
      avatarEl.innerHTML = `<span class="agent-cfg-avatar-symbol">${escapeHtml(formVals.emoji)}</span>`;
    }
  }

  if (titleEl) {
    titleEl.textContent = editingAgentId
      ? `${formVals.name || editingAgentId} 설정 및 하위 프롬프트 관리`
      : '새 에이전트 추가';
  }
  if (subEl) {
    if (editingAgentId) {
      let typeBadge;
      if (editingAgentId === CORE_ROOM_ID) {
        typeBadge = `<span class="agent-cfg-type-pill master">마스터 오케스트레이터</span>`;
      } else {
        const parentCfg = getAgentConfigById(formVals.parentAgentId);
        const parentName = parentCfg ? (parentCfg.name || formVals.parentAgentId) : '마스터';
        typeBadge = `<span class="agent-cfg-type-pill expert">소속: ${escapeHtml(parentName)} 산하</span>`;
      }
      subEl.innerHTML = `
        <span class="agent-cfg-id-badge">${escapeHtml(editingAgentId)}</span>
        ${typeBadge}
        <span class="agent-cfg-sub-desc">하위 세션 스폰 시 결합되는 프롬프트와 동작 정책을 투명하게 제어합니다</span>
      `;
    } else {
      subEl.innerHTML = `
        <span class="agent-cfg-type-pill new">신규 생성</span>
        <span class="agent-cfg-sub-desc">새로운 전문 에이전트의 역할, 작업 경로, 하위 세션 전달 프롬프트를 정의합니다</span>
      `;
    }
  }

  const setVal = (id, val) => {
    const el = document.getElementById(id);
    if (el) el.value = val !== undefined && val !== null ? String(val) : '';
  };

  setVal('agent-cfg-id', formVals.id);
  setVal('agent-cfg-name', formVals.name);
  setVal('agent-cfg-emoji', formVals.emoji);
  setVal('agent-cfg-role', formVals.roleDescription);
  setVal('agent-cfg-workdir', formVals.workdir);
  setVal('agent-cfg-child-prompt', formVals.childPromptTemplate);
  setVal('agent-cfg-sys-prompt', formVals.systemPrompt);

  const parentSel = document.getElementById('agent-cfg-parent');
  if (parentSel) {
    if (editingAgentId === CORE_ROOM_ID) {
      parentSel.innerHTML = `<option value="">(최상위 루트 오케스트레이터)</option>`;
      parentSel.value = '';
      parentSel.disabled = true;
    } else {
      const forbiddenIds = editingAgentId ? getDescendantAgentIds(editingAgentId) : new Set();
      if (editingAgentId) forbiddenIds.add(editingAgentId);

      const parentOptions = [
        `<option value="${CORE_ROOM_ID}">👑 마스터 직속 (${CORE_ROOM_ID})</option>`
      ];

      (agentConfigs || []).forEach(a => {
        if (!a || !a.id || a.id === CORE_ROOM_ID || forbiddenIds.has(a.id)) return;
        const icon = a.emoji || (a.id === EXPERT_VAULT_ROOM_ID ? '🧠' : (a.id === EXPERT_CODING_ROOM_ID ? '💻' : '🤖'));
        const label = `${icon} ${a.name || a.id} 산하 (${a.id})`;
        parentOptions.push(`<option value="${escapeHtml(a.id)}">${escapeHtml(label)}</option>`);
      });

      parentSel.innerHTML = parentOptions.join('');
      parentSel.value = formVals.parentAgentId || CORE_ROOM_ID;
      parentSel.disabled = isBuiltin;
    }
  }

  const modelSel = document.getElementById('agent-cfg-model');
  if (modelSel) {
    modelSel.innerHTML = getModelOptionsHtmlForAgentConfig(formVals.model);
    modelSel.value = formVals.model;
  }
  const childModelSel = document.getElementById('agent-cfg-child-model');
  if (childModelSel) {
    childModelSel.innerHTML = getModelOptionsHtmlForAgentConfig(formVals.childModel);
    childModelSel.value = formVals.childModel;
  }
  setVal('agent-cfg-effort', formVals.effort);
  setVal('agent-cfg-child-effort', formVals.childEffort);

  const injectChk = document.getElementById('agent-cfg-inject-child');
  if (injectChk) injectChk.checked = Boolean(formVals.injectChildTemplate);

  const resetBtn = document.getElementById('btn-agent-cfg-reset-builtin');
  if (resetBtn) resetBtn.style.display = isBuiltin ? 'inline-flex' : 'none';

  const delBtn = document.getElementById('btn-agent-cfg-delete-custom');
  if (delBtn) delBtn.style.display = isCustomExisting ? 'inline-flex' : 'none';

  const saveBtn = document.getElementById('btn-agent-cfg-save');
  if (saveBtn) saveBtn.textContent = editingAgentId ? '설정 저장' : '새 에이전트 생성';

  refreshAgentPromptPreview();
}

async function openAgentConfigModal(agentId = null, event = null) {
  if (event && typeof event.stopPropagation === 'function') {
    event.stopPropagation();
  }
  if (!Array.isArray(agentConfigs) || agentConfigs.length === 0) {
    await loadAgentConfigsFromServer();
  }
  const modal = document.getElementById('modal-agent-config');
  if (modal) modal.style.display = 'flex';
  switchAgentConfigTab('edit');
  populateAgentConfigForm(agentId);
}

function openCurrentRoomAgentConfigModal() {
  let targetId = CORE_ROOM_ID;
  if (currentRoomId && isOrchestratorAgentRoomId(currentRoomId)) {
    targetId = currentRoomId;
  } else {
    const curRoom = getCurrentRoom();
    if (curRoom && curRoom.parentAgentId && isOrchestratorAgentRoomId(curRoom.parentAgentId)) {
      targetId = curRoom.parentAgentId;
    }
  }
  openAgentConfigModal(targetId);
}

function closeAgentConfigModal() {
  const modal = document.getElementById('modal-agent-config');
  if (modal) modal.style.display = 'none';
}

function switchAgentConfigTab(tabName) {
  currentAgentConfigTab = (tabName === 'preview') ? 'preview' : 'edit';
  const editBtn = document.getElementById('tab-agent-cfg-edit');
  const prevBtn = document.getElementById('tab-agent-cfg-preview');
  const editPane = document.getElementById('agent-cfg-pane-edit');
  const prevPane = document.getElementById('agent-cfg-pane-preview');

  if (editBtn) editBtn.classList.toggle('active', currentAgentConfigTab === 'edit');
  if (prevBtn) prevBtn.classList.toggle('active', currentAgentConfigTab === 'preview');
  if (editPane) editPane.style.display = currentAgentConfigTab === 'edit' ? 'block' : 'none';
  if (prevPane) prevPane.style.display = currentAgentConfigTab === 'preview' ? 'block' : 'none';

  if (currentAgentConfigTab === 'preview') {
    refreshAgentPromptPreview();
  }
}

function setAgentConfigWorkdirPreset(preset) {
  const workdirInput = document.getElementById('agent-cfg-workdir');
  if (!workdirInput) return;
  if (preset === 'code') {
    workdirInput.value = (serverStatus && (serverStatus.codeDir || serverStatus.codedir || serverStatus.vaultDir)) || '';
  } else {
    workdirInput.value = (serverStatus && (serverStatus.vaultDir || serverStatus.workdir)) || '';
  }
  onAgentConfigFieldInput();
}

function collectAgentConfigFormDraft() {
  const idVal = (document.getElementById('agent-cfg-id')?.value || '').trim();
  const nameVal = (document.getElementById('agent-cfg-name')?.value || '').trim();
  const emojiVal = (document.getElementById('agent-cfg-emoji')?.value || '🤖').trim() || '🤖';
  const parentVal = (document.getElementById('agent-cfg-parent')?.value || '').trim();
  const roleVal = (document.getElementById('agent-cfg-role')?.value || '').trim();
  const workdirVal = (document.getElementById('agent-cfg-workdir')?.value || '').trim();
  const modelVal = (document.getElementById('agent-cfg-model')?.value || 'gemini-3.8-flash').trim();
  const effortVal = (document.getElementById('agent-cfg-effort')?.value || 'medium').trim();
  const childModelVal = (document.getElementById('agent-cfg-child-model')?.value || modelVal).trim();
  const childEffortVal = (document.getElementById('agent-cfg-child-effort')?.value || effortVal).trim();
  const injectChild = Boolean(document.getElementById('agent-cfg-inject-child')?.checked);
  const childPrompt = document.getElementById('agent-cfg-child-prompt')?.value || '';
  const sysPrompt = document.getElementById('agent-cfg-sys-prompt')?.value || '';

  return {
    id: idVal || editingAgentId || '',
    name: nameVal,
    emoji: emojiVal,
    parentAgentId: idVal === CORE_ROOM_ID ? null : (parentVal || CORE_ROOM_ID),
    roleDescription: roleVal,
    workdir: workdirVal,
    model: modelVal,
    effort: effortVal,
    childModel: childModelVal,
    childEffort: childEffortVal,
    injectChildTemplate: injectChild,
    childPromptTemplate: childPrompt,
    systemPrompt: sysPrompt,
  };
}

function onAgentConfigFieldInput() {
  const draft = collectAgentConfigFormDraft();
  const avatarEl = document.getElementById('agent-cfg-modal-avatar');
  if (avatarEl) {
    if (!editingAgentId) {
      avatarEl.innerHTML = `<svg class="i" viewBox="0 0 24 24"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>`;
    } else if (!draft.emoji || draft.emoji === '🤖' || ['👑', '🧠', '💻'].includes(draft.emoji)) {
      avatarEl.innerHTML = getAgentMonolineIconSvg(editingAgentId);
    } else {
      avatarEl.innerHTML = `<span class="agent-cfg-avatar-symbol">${escapeHtml(draft.emoji)}</span>`;
    }
  }

  if (agentPromptPreviewTimer) clearTimeout(agentPromptPreviewTimer);
  agentPromptPreviewTimer = setTimeout(() => {
    refreshAgentPromptPreview();
  }, 150);
}

async function refreshAgentPromptPreview() {
  const draft = collectAgentConfigFormDraft();
  const sampleTask = (document.getElementById('agent-cfg-preview-sample')?.value || '').trim()
    || '이번 달 지출 내역을 점검하고 특이사항을 요약해줘.';

  const badgeEl = document.getElementById('agent-preview-inject-badge');
  if (badgeEl) {
    const active = Boolean(draft.injectChildTemplate && draft.childPromptTemplate.trim());
    badgeEl.textContent = active ? '하위 지침 자동 주입 ON' : '원문 지시만 전달 (주입 OFF)';
    badgeEl.classList.toggle('is-off', !active);
  }

  try {
    const res = await fetch('/api/orchestrator/agents/preview', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        agentId: draft.id || CORE_ROOM_ID,
        sampleTask: sampleTask,
        draft: draft,
      })
    });
    const data = await res.json();
    if (data && data.ok && data.preview) {
      const p = data.preview;
      const assembledEl = document.getElementById('agent-preview-assembled-prompt');
      if (assembledEl) assembledEl.textContent = p.assembledChildUserPrompt || '';

      const cliEl = document.getElementById('agent-preview-cli-cmd');
      if (cliEl) {
        cliEl.textContent = [
          `# 1) 상위 에이전트가 하위 세션을 스폰할 때 호출하는 명령:`,
          p.spawnCliExample || '',
          ``,
          `# 2) 백엔드 에이전트 엔진이 실제 실행하는 하위 세션 CLI:`,
          p.childCliInvocation || '',
          `# 작업 디렉토리(cwd): ${p.childWorkdir || draft.workdir || ''}`,
          `# 기본 모델/Effort: ${p.childModel || draft.childModel} (${p.childEffort || draft.childEffort})`,
        ].join('\n');
      }

      const sysEl = document.getElementById('agent-preview-agent-sys');
      if (sysEl) sysEl.textContent = p.agentRuntimeSystemPrompt || draft.systemPrompt || '';
    }
  } catch (err) {
    console.warn('프롬프트 미리보기 조회 실패:', err);
  }
}

function copyAgentPreviewAssembledPrompt() {
  const el = document.getElementById('agent-preview-assembled-prompt');
  if (el && el.textContent) {
    copyToClipboard(el.textContent, '하위 채팅 조립 프롬프트가', true);
  }
}

async function saveAgentConfigModal() {
  const draft = collectAgentConfigFormDraft();
  if (!draft.name) {
    showToast('에이전트 이름을 입력해 주세요.');
    document.getElementById('agent-cfg-name')?.focus();
    return;
  }

  const saveBtn = document.getElementById('btn-agent-cfg-save');
  if (saveBtn) saveBtn.disabled = true;

  try {
    const res = await fetch('/api/orchestrator/agents', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        action: 'upsert',
        ...draft,
      })
    });
    const data = await res.json();
    if (data && data.ok && data.agent) {
      syncAgentConfigsState(data.agents, data.orchestratorRoomIds);
      populateAgentConfigForm(data.agent.id);
      showToast(`'${data.agent.name}' 에이전트 설정이 저장되었습니다.`);
    } else {
      showToast('저장 실패: ' + ((data && data.error) || '알 수 없는 오류'));
    }
  } catch (err) {
    showToast('저장 요청 중 오류: ' + (err.message || String(err)));
  } finally {
    if (saveBtn) saveBtn.disabled = false;
  }
}

async function resetCurrentBuiltinAgentConfig() {
  if (!editingAgentId) return;
  const cfg = getAgentConfigById(editingAgentId);
  const label = (cfg && cfg.name) || editingAgentId;
  openConfirmModal(
    '에이전트 설정 초기값 복원',
    `'${label}' 에이전트의 역할, 모델, 시스템 지침 및 하위 채팅 프롬프트 템플릿을 모두 초기 기본값으로 되돌리시겠습니까?`,
    '기본값 복원',
    async () => {
      try {
        const res = await fetch('/api/orchestrator/agents', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            action: 'reset',
            agentId: editingAgentId,
          })
        });
        const data = await res.json();
        if (data && data.ok && data.agent) {
          syncAgentConfigsState(data.agents, data.orchestratorRoomIds);
          populateAgentConfigForm(data.agent.id);
          if (typeof updateRoomConfigBar === 'function') {
            updateRoomConfigBar();
          }
          showToast(`'${data.agent.name}' 설정을 초기 기본값으로 복원했습니다.`);
        } else {
          showToast('복원 실패: ' + ((data && data.error) || '오류'));
        }
      } catch (err) {
        showToast('복원 중 오류: ' + err.message);
      }
    }
  );
}

async function deleteCurrentCustomAgentConfig() {
  if (!editingAgentId) return;
  const cfg = getAgentConfigById(editingAgentId);
  if (!cfg || cfg.builtin) {
    showToast('기본 내장 에이전트는 삭제할 수 없습니다.');
    return;
  }
  const targetId = editingAgentId;
  const label = cfg.name || targetId;
  openConfirmModal(
    '에이전트 삭제',
    `'${label}'(${targetId}) 에이전트를 삭제하시겠습니까?\n(등록된 지침과 설정이 제거되며, 산하 대화방 및 하위 에이전트는 마스터 직속으로 안전하게 보존됩니다)`,
    '에이전트 삭제',
    async () => {
      try {
        const res = await fetch('/api/orchestrator/agents/delete', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            agentId: targetId,
            deleteRoom: true,
          })
        });
        const data = await res.json();
        if (data && data.ok) {
          chatRooms = chatRooms.filter(r => r && r.id !== targetId);
          syncAgentConfigsState(data.agents, data.orchestratorRoomIds);
          if (currentRoomId === targetId) {
            selectRoom(CORE_ROOM_ID);
          }
          closeAgentConfigModal();
          showToast(`'${label}' 에이전트가 삭제되었습니다.`);
        } else {
          showToast('삭제 실패: ' + ((data && data.error) || '오류'));
        }
      } catch (err) {
        showToast('삭제 중 오류: ' + err.message);
      }
    }
  );
}
