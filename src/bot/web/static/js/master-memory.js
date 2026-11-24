// ─── 마스터 에이전트 모니터 인스펙터 ───
let latestMasterGraph = null;
let latestMasterTasks = [];
let latestMasterMemory = null;
let currentMsbMemoryTier = 'hot'; // 'hot', 'warm', 'cold', 'freeze'
const STORAGE_KEY_ARCHIVED_OPEN = 'oshyun_archived_open';
let isArchivedSectionOpen = false;
try {
  isArchivedSectionOpen = localStorage.getItem(STORAGE_KEY_ARCHIVED_OPEN) === 'true';
} catch (_) {}

function updateArchivedSectionDom() {
  const listEl = document.getElementById('sb-archived-list');
  const arrowEl = document.getElementById('sb-archived-arrow');
  if (listEl) listEl.classList.toggle('open', isArchivedSectionOpen);
  if (arrowEl) arrowEl.style.transform = isArchivedSectionOpen ? 'rotate(180deg)' : '';
}

function toggleArchivedSection() {
  isArchivedSectionOpen = !isArchivedSectionOpen;
  try {
    localStorage.setItem(STORAGE_KEY_ARCHIVED_OPEN, String(isArchivedSectionOpen));
  } catch (_) {}
  updateArchivedSectionDom();
}

async function toggleArchiveRoom(roomId, forceState = null) {
  if (!roomId || ORCHESTRATOR_ROOM_IDS.includes(roomId)) return;
  const targetRoom = getRoomById(roomId);
  const nextState = forceState !== null ? Boolean(forceState) : !Boolean(targetRoom && targetRoom.isArchived);
  try {
    const res = await fetch('/api/chat/rooms/' + encodeURIComponent(roomId) + '/archive', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ isArchived: nextState })
    });
    const data = await res.json();
    if (data.ok) {
      if (targetRoom) targetRoom.isArchived = nextState;
      for (const r of chatRooms) {
        if (r.id === roomId || r.sessionId === roomId) r.isArchived = nextState;
      }
      saveChatRoomsToStorage();
      renderRoomList();
      showToast(nextState ? '대화방이 보관되었습니다.' : '대화방 보관이 해제되었습니다.');
      loadMasterGraphFromServer();
    } else {
      showToast(data.error || '보관 상태 변경 실패');
    }
  } catch (err) {
    console.error('방 보관 처리 실패:', err);
    showToast('보관 상태 변경 중 오류가 발생했습니다.');
  }
}

function applyMasterModeElements() {
  const isMaster = (currentRoomId === CORE_ROOM_ID);
  const viewChat = document.getElementById('view-chat');
  const toggleBtn = document.getElementById('btn-toggle-master-sidebar');
  const memPillBtn = document.getElementById('btn-topbar-memory-pill');
  if (viewChat) viewChat.classList.toggle('master-mode', isMaster);
  if (toggleBtn) toggleBtn.style.display = isMaster ? 'inline-flex' : 'none';
  if (memPillBtn) memPillBtn.style.display = 'inline-flex';
  return isMaster;
}

function getMemoryScopeForAgentRoomId(roomId) {
  if (!roomId) return null;
  if (roomId === CORE_ROOM_ID) return 'master';
  if (roomId === EXPERT_VAULT_ROOM_ID) return 'second_brain';
  if (roomId === EXPERT_CODING_ROOM_ID) return 'bot_dev';
  const clean = String(roomId).trim();
  if (clean.startsWith('__custom_') && clean.endsWith('__')) {
    return clean.replace(/^__+|__+$/g, '');
  }
  return null;
}

function renderMsbMemoryAgentTabs() {
  const wrap = document.getElementById('msb-agent-tabs');
  if (!wrap) return;
  const baseTabs = [
    {
      scope: 'master',
      label: '마스터',
      icon: `<svg class="i" viewBox="0 0 24 24" style="width:13px;height:13px;"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="3"/><line x1="12" y1="3" x2="12" y2="6"/><line x1="12" y1="18" x2="12" y2="21"/></svg>`,
    },
    {
      scope: 'second_brain',
      label: '세컨브레인',
      icon: `<svg class="i" viewBox="0 0 24 24" style="width:13px;height:13px;"><path d="M9.5 2A2.5 2.5 0 0 1 12 4.5v15a2.5 2.5 0 0 1-4.96.44 2.5 2.5 0 0 1-2.96-3.08 3 3 0 0 1-.34-5.58 2.5 2.5 0 0 1 1.32-4.24 2.5 2.5 0 0 1 4.44-2.04"/><path d="M14.5 2A2.5 2.5 0 0 0 12 4.5v15a2.5 2.5 0 0 0 4.96.44 2.5 2.5 0 0 0 2.96-3.08 3 3 0 0 0 .34-5.58 2.5 2.5 0 0 0-1.32-4.24 2.5 2.5 0 0 0-4.44-2.04"/></svg>`,
    },
    {
      scope: 'bot_dev',
      label: '봇개발',
      icon: `<svg class="i" viewBox="0 0 24 24" style="width:13px;height:13px;"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>`,
    },
  ];
  if (Array.isArray(agentConfigs)) {
    for (const cfg of agentConfigs) {
      if (!cfg || !cfg.id || cfg.builtin) continue;
      const scope = getMemoryScopeForAgentRoomId(cfg.id);
      if (scope) {
        baseTabs.push({
          scope,
          label: cfg.name || cfg.id,
          icon: `<svg class="i" viewBox="0 0 24 24" style="width:13px;height:13px;"><rect x="3" y="11" width="18" height="10" rx="2"/><circle cx="12" cy="5" r="2"/><path d="M12 7v4"/><line x1="8" y1="16" x2="8" y2="16"/><line x1="16" y1="16" x2="16" y2="16"/></svg>`,
        });
      }
    }
  }
  wrap.innerHTML = baseTabs.map(t => {
    const isActive = (t.scope === currentMsbAgent);
    const safeScope = escapeHtml(t.scope);
    return `<button type="button" class="btn sm tab ${isActive ? 'active' : ''}" data-agent="${safeScope}" onclick="switchMsbMemoryAgent('${safeScope}')" style="flex:1;min-width:72px;font-size:11px;padding:4px 6px;display:inline-flex;align-items:center;justify-content:center;gap:5px;">${t.icon}<span>${escapeHtml(t.label)}</span></button>`;
  }).join('');
}

function getAppropriateMemoryAgentForCurrentContext() {
  // 1. 특정 에이전트 전용 대화방
  const directScope = getMemoryScopeForAgentRoomId(currentRoomId);
  if (directScope) return directScope;

  // 2. 오케스트레이터가 스폰한 하위 세션 (parentAgentId 기반 상속)
  const curRoom = Array.isArray(chatRooms) ? chatRooms.find(r => r.id === currentRoomId) : null;
  if (curRoom && curRoom.parentAgentId) {
    const parentScope = getMemoryScopeForAgentRoomId(curRoom.parentAgentId);
    if (parentScope) return parentScope;
  }

  // 3. 특정 화면/뷰 기준 자연스러운 에이전트 매핑
  if (currentView === 'vault') {
    return 'second_brain'; // 옵시디언 볼트 뷰 -> 세컨브레인 에이전트
  }

  // 4. 일반 대화방 및 기타 화면 -> 현재 선택된 에이전트 유지 또는 기본값 'master'
  return currentMsbAgent || 'master';
}

function openMasterMemoryInspector(targetAgent = null) {
  const agent = targetAgent || getAppropriateMemoryAgentForCurrentContext();
  switchMsbMemoryAgent(agent);
  toggleMasterSidebar(true);
  setTimeout(() => {
    const sec = document.getElementById('msb-memory-section');
    if (sec && typeof sec.scrollIntoView === 'function') {
      sec.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  }, 60);
}

function toggleMasterSidebar(forceState = null) {
  applyMasterModeElements();
  const sidebar = document.getElementById('master-sidebar');
  const backdrop = document.getElementById('master-drawer-backdrop');
  const memPillBtn = document.getElementById('btn-topbar-memory-pill');
  if (!sidebar) return;

  const willOpen = (forceState !== null) ? Boolean(forceState) : !sidebar.classList.contains('open');

  if (willOpen) {
    if (typeof closeThreadPanel === 'function') closeThreadPanel();
    if (typeof closeNoteViewer === 'function') closeNoteViewer();
    const targetAgent = getAppropriateMemoryAgentForCurrentContext();
    if (targetAgent && targetAgent !== currentMsbAgent) {
      switchMsbMemoryAgent(targetAgent);
    }
  }

  sidebar.classList.toggle('open', willOpen);
  if (backdrop) {
    if (window.innerWidth <= 1024) {
      backdrop.classList.toggle('open', willOpen);
    } else {
      backdrop.classList.remove('open');
    }
  }
  if (memPillBtn) {
    memPillBtn.classList.toggle('active', willOpen);
  }

  if (willOpen) {
    if (latestMasterGraph) {
      renderMasterSidebarContent(latestMasterGraph);
    } else {
      loadMasterGraphFromServer();
    }
    loadMasterTasksFromServer();
    loadMasterMemoryFromServer();
  }
}


async function loadMasterGraphFromServer() {
  try {
    const res = await fetch('/api/orchestrator/graph');
    const data = await res.json();
    if (data.ok && data.graph) {
      latestMasterGraph = data.graph;
      renderMasterSidebarContent(data.graph);
      updateCoreAgentSlotUi();
      renderRoomList();
    }
  } catch (err) {
    console.warn('마스터 상태 로드 실패:', err);
  }
}

async function loadMasterTasksFromServer() {
  try {
    const res = await fetch('/api/orchestrator/tasks?limit=100&include_closed=1');
    const data = await res.json();
    if (data.ok && Array.isArray(data.tasks)) {
      latestMasterTasks = data.tasks;
      // 현재 보고 있는 방은 낙관적 읽음(reviewed) 상태 유지 (서버 반영 레이스 컨디션 방어)
      if (currentRoomId) {
        latestMasterTasks.forEach(t => {
          if (t && t.roomId === currentRoomId) {
            t.reviewed = true;
          }
        });
      }
      updateMasterTopbarBadge();
      renderRoomList();
    }
  } catch (err) {
    console.warn('위임 작업 로드 실패:', err);
  }
}

function handleMasterGraphWsEvent(graph) {
  if (!graph) return;
  latestMasterGraph = graph;
  // 현재 보고 있는 방은 낙관적 읽음(reviewed) 상태 유지
  if (currentRoomId && Array.isArray(latestMasterTasks)) {
    latestMasterTasks.forEach(t => {
      if (t && t.roomId === currentRoomId) {
        t.reviewed = true;
      }
    });
  }
  renderMasterSidebarContent(graph);
  updateCoreAgentSlotUi();
  renderRoomList();
}

function isPendingMasterTask(t) {
  if (!t || t.reviewed) return false;
  if (['queued', 'running', 'stalled', 'failed', 'lost'].includes(t.status)) return true;
  if (t.status === 'done') return !(t.reportedAt || t.wait);
  return false;
}

function updateMasterTopbarBadge() {
  const btn = document.getElementById('btn-toggle-master-sidebar');
  const badge = document.getElementById('msb-topbar-badge');
  if (!btn || !badge) return;
  const s = (latestMasterGraph && latestMasterGraph.summary) || {};
  const activeTasksCount = latestMasterTasks.filter(isPendingMasterTask).length;
  const runningCount = s.running || 0;
  const attentionCount = s.attention || 0;
  const displayCount = runningCount + activeTasksCount;
  if (displayCount > 0) {
    badge.textContent = String(displayCount);
    badge.style.display = 'inline-flex';
  } else {
    badge.style.display = 'none';
  }
  btn.classList.toggle('has-attention', attentionCount > 0);
}

let currentMsbAgent = 'master';

function switchMsbMemoryAgent(agentId) {
  currentMsbAgent = agentId || 'master';
  const tabs = document.querySelectorAll('#msb-agent-tabs button');
  tabs.forEach(btn => {
    btn.classList.toggle('active', btn.dataset.agent === currentMsbAgent);
  });
  loadMasterMemoryFromServer();
}

async function loadMasterMemoryFromServer() {
  try {
    const res = await fetch(`/api/orchestrator/memory?agentId=${currentMsbAgent}`);
    const data = await res.json();
    if (data && data.ok) {
      renderMsbMemory(data);
    }
  } catch (err) {
    console.warn('에이전트 메모리 로드 실패:', err);
  }
}

function selectMsbMemoryTier(tier) {
  const normalized = ['hot', 'warm', 'cold', 'freeze'].includes(tier) ? tier : 'hot';
  currentMsbMemoryTier = normalized;
  const nodes = document.querySelectorAll('#msb-mem-pipeline .msb-mem-node');
  nodes.forEach(btn => {
    btn.classList.toggle('active', btn.dataset.tier === normalized);
  });
  renderMsbMemory();
}

const MEMORY_AGENT_PARENT_MAP = {
  master: CORE_ROOM_ID,
  second_brain: EXPERT_VAULT_ROOM_ID,
  bot_dev: EXPERT_CODING_ROOM_ID
};
const MEMORY_AGENT_LABEL_MAP = {
  master: '마스터',
  second_brain: '세컨브레인',
  bot_dev: '봇개발'
};

async function rebalanceMasterMemory() {
  const agentId = MEMORY_AGENT_PARENT_MAP[currentMsbAgent] ? currentMsbAgent : 'master';
  const parentRoomId = MEMORY_AGENT_PARENT_MAP[agentId] || CORE_ROOM_ID;
  const agentLabel = MEMORY_AGENT_LABEL_MAP[agentId] || '마스터';
  const roomTitle = `${agentLabel} 메모리 리밸런싱`;
  const prompt = agentId === 'master'
    ? '/memory-rebalance --agent-id master'
    : `/memory-rebalance --agent-id ${agentId} (${agentLabel} 전용 메모리 대상)`;

  const btn = document.getElementById('btn-msb-mem-rebalance');
  if (btn) {
    btn.disabled = true;
    btn.textContent = '⏳ 스폰 중…';
  }
  try {
    const res = await fetch('/api/orchestrator/dispatch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        action: 'spawn',
        forceNew: true,
        parentAgentId: parentRoomId,
        title: roomTitle,
        prompt,
        rawPrompt: true,
        slashCommands: true,
        model: 'gemini-3.8-flash',
        effort: 'low',
        wait: false
      })
    });
    const data = await res.json();
    if (data && data.ok) {
      if (window.innerWidth <= 1024) {
        toggleMasterSidebar(false);
      }
      showToast(`${agentLabel} 하위 세션(${parentRoomId})으로 메모리 리밸런싱을 시작했습니다.`, { roomId: data.roomId || parentRoomId });
      await loadChatRoomsFromServer();
      loadMasterGraphFromServer();
      loadMasterTasksFromServer();
      if (data.roomId) {
        selectRoom(data.roomId);
      }
    } else {
      showToast('메모리 리밸런싱 스폰 실패: ' + ((data && data.error) || '알 수 없는 오류'));
    }
  } catch (err) {
    showToast('메모리 리밸런싱 스폰 중 오류: ' + (err.message || String(err)));
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = `<svg class="i" viewBox="0 0 24 24" style="width:13px;height:13px;"><path d="m16 16 3-8 3 8c-.87.65-1.92 1-3 1s-2.13-.35-3-1Z"/><path d="m2 16 3-8 3 8c-.87.65-1.92 1-3 1s-2.13-.35-3-1Z"/><path d="M7 21h10"/><path d="M12 3v18"/><path d="M3 7h2c2 0 5-1 7-2 2 1 5 2 7 2h2"/></svg><span>리밸런싱</span>`;
    }
  }
}

function applyOptimisticMemoryMove(snap, itemId, toTier) {
  if (!snap || !snap.tiers || !snap.tiers[toTier]) return null;
  const tiers = ['hot', 'warm', 'cold', 'freeze'];
  let fromTier = null;
  let foundIndex = -1;
  for (const t of tiers) {
    const items = snap.tiers[t] && Array.isArray(snap.tiers[t].items) ? snap.tiers[t].items : [];
    const idx = items.findIndex(it => it && it.id === itemId);
    if (idx !== -1) {
      fromTier = t;
      foundIndex = idx;
      break;
    }
  }
  if (!fromTier || fromTier === toTier || foundIndex === -1) return null;

  const nextSnap = JSON.parse(JSON.stringify(snap));
  const [movedItem] = nextSnap.tiers[fromTier].items.splice(foundIndex, 1);
  movedItem.fromTier = fromTier;
  movedItem.movedAt = new Date().toISOString();
  nextSnap.tiers[toTier].items.unshift(movedItem);

  let totalItems = 0;
  let totalChars = 0;
  for (const t of tiers) {
    const tierObj = nextSnap.tiers[t];
    if (!tierObj) continue;
    const items = Array.isArray(tierObj.items) ? tierObj.items : [];
    const charCount = items.reduce((acc, it) => acc + Number((it && it.chars) || (it && it.text ? it.text.length : 0) || 0), 0);
    tierObj.itemCount = items.length;
    tierObj.charCount = charCount;
    const maxChars = Number(tierObj.maxChars || 0);
    const maxItems = Number(tierObj.maxItems || 0);
    const charPct = maxChars > 0 ? Math.round((charCount / maxChars) * 100) : 0;
    const itemPct = maxItems > 0 ? Math.round((items.length / maxItems) * 100) : 0;
    tierObj.usagePct = Math.min(100, Math.max(charPct, itemPct));
    totalItems += items.length;
    totalChars += charCount;
  }
  nextSnap.totalItems = totalItems;
  nextSnap.totalChars = totalChars;
  return { nextSnap, fromTier };
}

async function moveMasterMemoryItem(itemId, toTier) {
  if (!itemId || !toTier) return;
  const syncMemorySnapshot = snap => {
    if (!snap) return;
    latestMasterMemory = snap;
    if (latestMasterGraph) latestMasterGraph.memory = snap;
    renderMsbMemory(snap);
  };
  const prevSnapshot = latestMasterMemory ? JSON.parse(JSON.stringify(latestMasterMemory)) : null;
  const optimistic = prevSnapshot ? applyOptimisticMemoryMove(prevSnapshot, itemId, toTier) : null;
  if (optimistic) syncMemorySnapshot(optimistic.nextSnap);

  try {
    const res = await fetch('/api/orchestrator/memory', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'move', itemId, toTier, agentId: currentMsbAgent })
    });
    const data = await res.json();
    if (data && data.ok) {
      syncMemorySnapshot(data);
      showToast(`항목을 ${toTier.toUpperCase()} 계층으로 이동했습니다.`);
    } else {
      syncMemorySnapshot(prevSnapshot);
      showToast('이동 실패: ' + ((data && data.error) || '알 수 없는 오류'));
    }
  } catch (err) {
    syncMemorySnapshot(prevSnapshot);
    showToast('이동 실패: ' + err.message);
  }
}

async function deleteMasterMemoryItem(itemId) {
  if (!itemId) return;
  try {
    const res = await fetch('/api/orchestrator/memory', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'delete', itemId, agentId: currentMsbAgent })
    });
    const data = await res.json();
    if (data && data.ok) {
      latestMasterMemory = data;
      if (latestMasterGraph) latestMasterGraph.memory = data;
      renderMsbMemory(data);
      showToast('메모리 항목을 삭제했습니다.');
    } else {
      showToast('삭제 실패: ' + ((data && data.error) || '알 수 없는 오류'));
    }
  } catch (err) {
    showToast('삭제 실패: ' + err.message);
  }
}

async function clearMasterMemory(tier = currentMsbMemoryTier) {
  const target = ['hot', 'warm', 'cold', 'freeze'].includes(tier) ? tier : 'all';
  const label = target === 'all' ? '4계층 전체' : target.toUpperCase();
  openConfirmModal(
    '마스터 메모리 비우기',
    `${label} 메모리 영역의 항목을 비우시겠습니까?`,
    '비우기',
    async () => {
      try {
        const res = await fetch('/api/orchestrator/memory', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: 'clear', tier: target, agentId: currentMsbAgent })
        });
        const data = await res.json();
        if (data && data.ok) {
          latestMasterMemory = data;
          if (latestMasterGraph) latestMasterGraph.memory = data;
          renderMsbMemory(data);
          showToast(`${label} 메모리를 비웠습니다.`);
        } else {
          showToast('메모리 비우기 실패: ' + ((data && data.error) || '알 수 없는 오류'));
        }
      } catch (err) {
        showToast('메모리 비우기 실패: ' + err.message);
      }
    }
  );
}

function renderMsbMemory(memory = latestMasterMemory) {
  if (memory) latestMasterMemory = memory;
  const snap = memory || latestMasterMemory;
  const listEl = document.getElementById('msb-memory-list');
  const totalEl = document.getElementById('msb-memory-total');
  const usageTextEl = document.getElementById('msb-mem-usage-text');
  const fillEl = document.getElementById('msb-mem-meter-fill');

  if (!snap || !snap.tiers) {
    if (listEl) listEl.innerHTML = '<div class="msb-empty">메모리 정보가 없습니다.</div>';
    return;
  }

  if (totalEl) {
    totalEl.textContent = `총 ${snap.totalItems || 0}건 · ${snap.totalChars || 0}자`;
  }
  const pillTextEl = document.getElementById('topbar-memory-pill-text');
  if (pillTextEl) {
    pillTextEl.textContent = '메모리';
  }
  ['hot', 'warm', 'cold', 'freeze'].forEach(t => {
    const cntEl = document.getElementById(`msb-mem-cnt-${t}`);
    const miniFillEl = document.getElementById(`msb-mem-fill-${t}`);
    const tierObj = snap.tiers[t];
    if (cntEl) cntEl.textContent = String((tierObj && tierObj.itemCount) || 0);
    if (miniFillEl) {
      const tPct = Math.min(100, Math.max(0, Number((tierObj && tierObj.usagePct) || 0)));
      miniFillEl.style.width = `${tPct}%`;
    }
  });

  const cur = snap.tiers[currentMsbMemoryTier] || snap.tiers.hot || {};
  const itemCnt = cur.itemCount || 0;
  const maxItems = cur.maxItems || 0;
  const charCnt = cur.charCount || 0;
  const maxChars = cur.maxChars || 0;
  const pct = Math.min(100, Math.max(0, Number(cur.usagePct || 0)));

  if (usageTextEl) {
    usageTextEl.textContent = `${charCnt} / ${maxChars}자 (${itemCnt}/${maxItems}건 · ${pct}%)`;
  }
  if (fillEl) {
    fillEl.style.width = `${pct}%`;
    const colors = { hot: '#ef4444', warm: '#f59e0b', cold: '#3b82f6', freeze: '#64748b' };
    fillEl.style.backgroundColor = colors[currentMsbMemoryTier] || 'var(--accent)';
  }

  if (!listEl) return;
  initMsbMemoryDragAndDrop();
  const items = Array.isArray(cur.items) ? cur.items : [];
  if (items.length === 0) {
    listEl.innerHTML = `<div class="msb-empty">${currentMsbMemoryTier.toUpperCase()} 계층이 비어 있습니다.</div>`;
    return;
  }
  listEl.innerHTML = items.map(it => {
    const title = escapeHtml(it.title || it.kind || currentMsbMemoryTier.toUpperCase());
    const rawText = String(it.text || '');
    const textHtml = (typeof formatMarkdown === 'function')
      ? formatMarkdown(rawText)
      : escapeHtml(rawText).replace(/\n/g, '<br>');
    const when = it.movedAt ? `${it.fromTier ? it.fromTier.toUpperCase() + '→' : ''}${formatRelativeTime(it.movedAt)}` : formatRelativeTime(it.createdAt);

    return `
      <div class="msb-mem-card" draggable="true" data-item-id="${escapeHtml(it.id)}" data-tier="${escapeHtml(currentMsbMemoryTier)}">
        <div class="msb-mem-card-head">
          <div style="display:flex;align-items:center;gap:5px;min-width:0;">
            <span class="msb-mem-drag-handle" title="상단 계층 슬롯으로 드래그하여 이동" aria-hidden="true">⋮⋮</span>
            <span class="msb-mem-badge ${escapeHtml(currentMsbMemoryTier)}">${escapeHtml(currentMsbMemoryTier)}</span>
            <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:600;">${title}</span>
          </div>
          <span class="msb-mem-card-meta">${escapeHtml(when)} (${it.chars || 0}자)</span>
        </div>
        <div class="msb-mem-card-text md md-compact">${textHtml}</div>
        <div class="msb-mem-card-footer">
          <button type="button" class="btn xs ghost" onclick="deleteMasterMemoryItem('${escapeHtml(it.id)}')" title="이 메모리 삭제" style="color:var(--danger,#ef4444);padding:1px 4px;font-size:10px;">삭제</button>
        </div>
      </div>
    `;
  }).join('');
}

let msbMemoryDndInitialized = false;
function initMsbMemoryDragAndDrop() {
  if (msbMemoryDndInitialized) return;
  const listEl = document.getElementById('msb-memory-list');
  const pipelineEl = document.getElementById('msb-mem-pipeline');
  if (!listEl || !pipelineEl) return;
  msbMemoryDndInitialized = true;

  let activeDrag = null; // { itemId, fromTier, cardEl }
  let touchState = null; // { timer, active, itemId, fromTier, cardEl, startX, startY, ghostEl, hoveredNode }

  function setPipelineDragState(isActive, sourceTier = '') {
    pipelineEl.classList.toggle('is-drag-active', isActive);
    pipelineEl.querySelectorAll('.msb-mem-node').forEach(node => {
      node.classList.toggle('is-source-tier', isActive && node.dataset.tier === sourceTier);
      if (!isActive) node.classList.remove('drag-over');
    });
  }

  function createMemoryDragGhost(cardEl) {
    const ghost = document.createElement('div');
    ghost.className = 'msb-mem-drag-ghost';
    const titleEl = cardEl.querySelector('.msb-mem-card-head');
    ghost.textContent = titleEl ? titleEl.textContent.replace('⋮⋮', '').trim() : '메모리 이동';
    return ghost;
  }

  // 데스크톱 HTML5 Drag & Drop (카드 리스트 위임)
  listEl.addEventListener('dragstart', e => {
    const card = e.target.closest('.msb-mem-card[draggable="true"]');
    if (!card) return;
    const itemId = card.dataset.itemId;
    const fromTier = card.dataset.tier;
    if (!itemId || !fromTier) return;

    activeDrag = { itemId, fromTier, cardEl: card };
    if (e.dataTransfer) {
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', itemId);
      if (typeof e.dataTransfer.setDragImage === 'function') {
        const ghost = createMemoryDragGhost(card);
        ghost.classList.add('is-native-preview');
        document.body.appendChild(ghost);
        e.dataTransfer.setDragImage(ghost, 24, 16);
        requestAnimationFrame(() => {
          if (ghost.parentNode) ghost.parentNode.removeChild(ghost);
        });
      }
    }
    requestAnimationFrame(() => {
      card.classList.add('is-dragging');
      setPipelineDragState(true, fromTier);
    });
  });

  listEl.addEventListener('dragend', () => {
    if (activeDrag && activeDrag.cardEl) {
      activeDrag.cardEl.classList.remove('is-dragging');
    }
    activeDrag = null;
    setPipelineDragState(false);
  });

  // 상단 계층 슬롯 Drop Target
  const canDropOnNode = node => Boolean(activeDrag && node.dataset.tier && node.dataset.tier !== activeDrag.fromTier);
  pipelineEl.querySelectorAll('.msb-mem-node').forEach(node => {
    node.addEventListener('dragenter', e => {
      if (!canDropOnNode(node)) return;
      e.preventDefault();
      node.classList.add('drag-over');
    });

    node.addEventListener('dragover', e => {
      if (!canDropOnNode(node)) return;
      e.preventDefault();
      if (e.dataTransfer) e.dataTransfer.dropEffect = 'move';
      node.classList.add('drag-over');
    });

    node.addEventListener('dragleave', e => {
      if (!node.contains(e.relatedTarget)) {
        node.classList.remove('drag-over');
      }
    });

    node.addEventListener('drop', e => {
      e.preventDefault();
      const toTier = node.dataset.tier;
      const itemId = (activeDrag && activeDrag.itemId) || (e.dataTransfer && e.dataTransfer.getData('text/plain'));
      const fromTier = activeDrag && activeDrag.fromTier;
      if (activeDrag && activeDrag.cardEl) {
        activeDrag.cardEl.classList.remove('is-dragging');
      }
      activeDrag = null;
      setPipelineDragState(false);
      if (itemId && toTier && toTier !== fromTier) {
        moveMasterMemoryItem(itemId, toTier);
      }
    });
  });

  // 모바일 터치 드래그 지원 (핸들 즉시 드래그 / 카드 220ms 롱프레스 드래그)
  function cleanupTouchDrag() {
    if (!touchState) return;
    if (touchState.timer) clearTimeout(touchState.timer);
    if (touchState.ghostEl && touchState.ghostEl.parentNode) {
      touchState.ghostEl.parentNode.removeChild(touchState.ghostEl);
    }
    if (touchState.cardEl) touchState.cardEl.classList.remove('is-dragging');
    setPipelineDragState(false);
    touchState = null;
  }

  function activateTouchDrag(touch) {
    if (!touchState || touchState.active) return;
    touchState.active = true;
    touchState.cardEl.classList.add('is-dragging');
    setPipelineDragState(true, touchState.fromTier);
    if (navigator.vibrate) {
      try { navigator.vibrate(15); } catch (_) {}
    }
    const ghost = createMemoryDragGhost(touchState.cardEl);
    ghost.style.left = `${touch.clientX}px`;
    ghost.style.top = `${touch.clientY}px`;
    document.body.appendChild(ghost);
    touchState.ghostEl = ghost;
  }

  listEl.addEventListener('touchstart', e => {
    if (e.touches.length !== 1) return;
    if (e.target.closest('button')) return;
    const card = e.target.closest('.msb-mem-card[data-item-id]');
    if (!card) return;
    const touch = e.touches[0];
    const isHandle = Boolean(e.target.closest('.msb-mem-drag-handle'));
    touchState = {
      timer: null,
      active: false,
      itemId: card.dataset.itemId,
      fromTier: card.dataset.tier,
      cardEl: card,
      startX: touch.clientX,
      startY: touch.clientY,
      ghostEl: null,
      hoveredNode: null,
    };
    if (isHandle) {
      activateTouchDrag(touch);
    } else {
      touchState.timer = setTimeout(() => activateTouchDrag(touch), 220);
    }
  }, { passive: true });

  listEl.addEventListener('touchmove', e => {
    if (!touchState || e.touches.length !== 1) return;
    const touch = e.touches[0];
    if (!touchState.active) {
      const dx = Math.abs(touch.clientX - touchState.startX);
      const dy = Math.abs(touch.clientY - touchState.startY);
      if (dx > 8 || dy > 8) {
        cleanupTouchDrag();
      }
      return;
    }
    e.preventDefault();
    if (touchState.ghostEl) {
      touchState.ghostEl.style.left = `${touch.clientX}px`;
      touchState.ghostEl.style.top = `${touch.clientY}px`;
    }
    const hitEl = document.elementFromPoint(touch.clientX, touch.clientY);
    const targetNode = hitEl ? hitEl.closest('#msb-mem-pipeline .msb-mem-node') : null;
    if (touchState.hoveredNode && touchState.hoveredNode !== targetNode) {
      touchState.hoveredNode.classList.remove('drag-over');
    }
    if (targetNode && targetNode.dataset.tier !== touchState.fromTier) {
      targetNode.classList.add('drag-over');
      touchState.hoveredNode = targetNode;
    } else {
      touchState.hoveredNode = null;
    }
  }, { passive: false });

  listEl.addEventListener('touchend', () => {
    if (!touchState) return;
    const targetTier = touchState.active && touchState.hoveredNode ? touchState.hoveredNode.dataset.tier : null;
    const itemId = touchState.itemId;
    const fromTier = touchState.fromTier;
    cleanupTouchDrag();
    if (targetTier && targetTier !== fromTier && itemId) {
      moveMasterMemoryItem(itemId, targetTier);
    }
  });

  listEl.addEventListener('touchcancel', cleanupTouchDrag);
}

// 마스터 우측 인스펙터 내용 렌더링
function renderMasterSidebarContent(graph) {
  if (!graph) return;
  const s = graph.summary || {};
  const summaryEl = document.getElementById('msb-summary-text');
  if (summaryEl) {
    let text = `하위 세션 ${s.children || 0} · 실행 ${s.running || 0} · 대기 작업 ${s.activeTasks || 0}`;
    if (s.attention > 0) text += ` · 확인 ${s.attention}`;
    summaryEl.textContent = text;
  }
  updateMasterTopbarBadge();
  if (graph.memory) renderMsbMemory(graph.memory);
}


async function triggerManualPatrol() {
  try {
    const res = await fetch('/api/orchestrator/patrol/trigger', { method: 'POST' });
    const data = await res.json();
    if (data.ok) {
      showToast(data.message || '하위 에이전트 상태 점검이 완료되었습니다.');
    } else {
      showToast('하위 에이전트 점검 요청 실패: ' + (data.error || '실패'));
    }
    loadMasterGraphFromServer();
  } catch (err) {
    showToast('하위 에이전트 점검 요청 중 네트워크 오류: ' + err);
  }
}

function openSpawnSessionModal() {
  const modal = document.getElementById('modal-spawn-session');
  if (modal) modal.style.display = 'flex';
  const titleInput = document.getElementById('input-spawn-session-title');
  if (titleInput) { titleInput.value = ''; titleInput.focus(); }
  const promptInput = document.getElementById('input-spawn-session-prompt');
  if (promptInput) promptInput.value = '';
}

async function submitSpawnSession() {
  const titleInput = document.getElementById('input-spawn-session-title');
  const promptInput = document.getElementById('input-spawn-session-prompt');
  const title = (titleInput ? titleInput.value : '').trim();
  const prompt = (promptInput ? promptInput.value : '').trim();
  if (!prompt) {
    showToast('초기 지시 메시지를 입력해주세요.');
    return;
  }
  const submitBtn = document.querySelector('#modal-spawn-session .modal-footer .btn.primary');
  if (submitBtn) submitBtn.disabled = true;
  const parentRoomId = (currentRoomId && ORCHESTRATOR_ROOM_IDS.includes(currentRoomId)) ? currentRoomId : CORE_ROOM_ID;
  try {
    const res = await fetch('/api/orchestrator/dispatch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        action: 'spawn',
        parentAgentId: parentRoomId,
        title: title || '새 에이전트 세션',
        prompt: prompt,
        wait: false
      })
    });
    const data = await res.json();
    if (data.ok) {
      closeModal('modal-spawn-session');
      showToast(`새 세션 '${data.title || title}' 생성 및 위임 완료`);
      await loadChatRoomsFromServer();
      loadMasterGraphFromServer();
      loadMasterTasksFromServer();
      if (data.roomId) {
        selectRoom(data.roomId);
      }
    } else {
      showToast('스폰 실패: ' + (data.error || '오류'));
    }
  } catch (err) {
    showToast('스폰 중 오류: ' + err);
  } finally {
    if (submitBtn) submitBtn.disabled = false;
  }
}

function openTaskNoteModal(taskId) {
  const modal = document.getElementById('modal-task-note');
  if (modal) modal.style.display = 'flex';
  const idInput = document.getElementById('input-task-note-task-id');
  if (idInput) idInput.value = taskId;
  const noteInput = document.getElementById('input-task-note-text');
  if (noteInput) { noteInput.value = ''; noteInput.focus(); }
}

async function submitTaskNote() {
  const idInput = document.getElementById('input-task-note-task-id');
  const noteInput = document.getElementById('input-task-note-text');
  const closeCb = document.getElementById('input-task-note-close');
  const taskId = (idInput ? idInput.value : '').trim();
  const note = (noteInput ? noteInput.value : '').trim();
  const close = Boolean(closeCb && closeCb.checked);
  if (!taskId) return;
  const submitBtn = document.querySelector('#modal-task-note .modal-footer .btn.primary');
  if (submitBtn) submitBtn.disabled = true;
  try {
    const res = await fetch('/api/orchestrator/task/update', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ taskId, note, close })
    });
    const data = await res.json();
    if (data.ok) {
      closeModal('modal-task-note');
      showToast('작업이 갱신되었습니다.');
      loadMasterTasksFromServer();
      loadMasterGraphFromServer();
    } else {
      showToast('갱신 실패: ' + (data.error || '오류'));
    }
  } catch (err) {
    showToast('갱신 중 오류: ' + err);
  } finally {
    if (submitBtn) submitBtn.disabled = false;
  }
}

// ─── 빈 새 대화방 정리 유틸 ───
function isUnusedEmptyRoom(room) {
  if (!room || !room.id || ORCHESTRATOR_ROOM_IDS.includes(room.id)) return false;
  if ((room.messageCount || 0) > 0) return false;
  if (room.byOrchestrator || room.isOrchestratorChild) return false;
  const msgs = Array.isArray(room.messages) ? room.messages : [];
  const hasRealMsg = msgs.some(m => m && (m.text || (m.files && m.files.length) || (m.attachments && m.attachments.length)));
  if (hasRealMsg) return false;
  if (room.sessionId) return false;
  const live = (typeof roomLiveStates !== 'undefined' && roomLiveStates) ? roomLiveStates[room.id] : null;
  if (live && live.isRunning) return false;
  if (room.id === currentRoomId) {
    if ((typeof pendingAttachments !== 'undefined' && pendingAttachments.length > 0) ||
        (typeof pendingLongPastes !== 'undefined' && pendingLongPastes.length > 0)) {
      return false;
    }
    const chatInput = document.getElementById('chat-input');
    if (chatInput && typeof getRawChatInputTypedText === 'function' && getRawChatInputTypedText(chatInput).trim()) {
      return false;
    }
  }
  return true;
}

function cleanUnusedEmptyRooms(activeRoomId = null) {
  if (typeof isFilePickerActive !== 'undefined' && isFilePickerActive) return;
  if (!Array.isArray(chatRooms)) return;
  const orchIds = (typeof ORCHESTRATOR_ROOM_IDS !== 'undefined' && Array.isArray(ORCHESTRATOR_ROOM_IDS))
    ? ORCHESTRATOR_ROOM_IDS
    : [CORE_ROOM_ID, '__expert_vault__', '__expert_coding__'];
  const beforeLen = chatRooms.length;
  chatRooms = chatRooms.filter(r => {
    if (!r) return false;
    if (activeRoomId && r.id === activeRoomId) return true;
    if (orchIds.includes(r.id)) return true;
    return !isUnusedEmptyRoom(r);
  });
  if (chatRooms.length !== beforeLen) {
    saveChatRoomsToStorage(true);
    if (!orchIds.includes(currentRoomId) && !chatRooms.some(r => r.id === currentRoomId)) {
      currentRoomId = chatRooms.length > 0 ? chatRooms[0].id : CORE_ROOM_ID;
    }
    renderRoomList();
  }
}

function cleanCurrentIfUnusedEmpty() {
  if (typeof isFilePickerActive !== 'undefined' && isFilePickerActive) return;
  const cur = getCurrentRoom();
  if (cur && isUnusedEmptyRoom(cur)) {
    cleanUnusedEmptyRooms(null);
    if (chatRooms.length > 0) {
      currentRoomId = chatRooms[0].id;
    } else {
      currentRoomId = CORE_ROOM_ID;
    }
    saveChatRoomsToStorage(true);
    renderRoomList();
    renderMessagesForCurrentRoom(true);
    updateTopbarTitle();
    updateSessionBar();
  }
}

// ─── 뷰 전환 ───
function switchView(viewName) {
  if (viewName !== 'chat') {
    cleanCurrentIfUnusedEmpty();
    if (window.innerWidth <= 1024) {
      toggleMasterSidebar(false);
    }
  }
  currentView = viewName;
  document.querySelectorAll('.view').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
  const targetView = document.getElementById('view-' + viewName);
  const targetTab = document.getElementById('tab-' + viewName);
  if (targetView) targetView.classList.add('active');
  if (targetTab) targetTab.classList.add('active');

  const topbar = document.getElementById('topbar');
  const chatActions = document.getElementById('chat-actions');
  const editHint = document.getElementById('topbar-edit-hint');
  if (topbar) topbar.classList.toggle('bordered', viewName !== 'chat');
  if (chatActions) chatActions.style.display = viewName === 'chat' ? 'flex' : 'none';
  if (editHint) editHint.style.display = viewName === 'chat' ? '' : 'none';
  updateTopbarTitle();
  closeRoomDrawer();
  if (typeof resetPullToRefreshIndicator === 'function') {
    resetPullToRefreshIndicator(true);
  } else {
    const ptr = document.getElementById('ptr-indicator');
    if (ptr) {
      ptr.classList.remove('pulling', 'ready', 'refreshing', 'returning', 'from-bottom');
      ptr.style.transform = '';
      ptr.style.opacity = '';
      ptr.style.top = '';
      ptr.style.bottom = '';
    }
  }
  const pinchHud = document.getElementById('pinch-font-hud');
  if (pinchHud) pinchHud.classList.remove('visible');

  if (viewName === 'vault') refreshVaultTree();
  else if (viewName === 'automation') { if (typeof loadAutomationData === 'function') loadAutomationData(); }
  else if (viewName === 'settings') { loadStatusAndSettings(); updatePwaUi(); updateNotificationUi(); updateMobileAutofocusUi(); updateUiFontSizeUi(); updateFontSizeUi(); }
  else if (viewName === 'chat') {
    if (chatRooms.length === 0) {
      createNewChat();
    } else if (!currentRoomId || (currentRoomId !== CORE_ROOM_ID && !chatRooms.some(r => r.id === currentRoomId))) {
      selectRoom(chatRooms[0].id);
    }
    syncMasterModeUi();
    focusChatInput();
    scrollToBottomIfNear(true);
  }
}

function updateTopbarTitle() {
  const titleEl = document.getElementById('topbar-title');
  if (!titleEl) return;
  if (currentView === 'chat') {
    const cur = getCurrentRoom();
    titleEl.innerText = (cur && cur.title) ? cur.title : '새 대화';
  } else {
    titleEl.innerText = VIEW_TITLES[currentView] || 'second-bot';
  }
}
