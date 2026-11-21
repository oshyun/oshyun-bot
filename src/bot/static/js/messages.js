function onMessagesScroll() {
  updateScrollBottomButton();
}

function updateScrollBottomButton() {
  const container = document.getElementById('messages-container');
  const btn = document.getElementById('btn-scroll-bottom');
  if (!container || !btn) return;
  const dist = container.scrollHeight - container.scrollTop - container.clientHeight;
  btn.classList.toggle('show', dist > 180);
}

function scrollToBottom(smooth = false) {
  const container = document.getElementById('messages-container');
  if (!container) return;
  const doScroll = () => {
    if (!container) return;
    if (smooth && typeof container.scrollTo === 'function') {
      container.scrollTo({ top: container.scrollHeight, behavior: 'smooth' });
    } else {
      container.scrollTop = container.scrollHeight;
    }
    updateScrollBottomButton();
  };
  doScroll();
  requestAnimationFrame(doScroll);
  setTimeout(doScroll, 60);
  setTimeout(doScroll, 180);
}

function scrollToBottomIfNear(force = false) {
  const container = document.getElementById('messages-container');
  if (!container) return;
  const nearBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 160;
  if (force || nearBottom) {
    scrollToBottom(false);
  } else {
    updateScrollBottomButton();
  }
}

function isSubAgentReportMessage(m) {
  if (!m || typeof m !== 'object') return false;
  const role = m.sender || m.role || '';
  if (role === 'assistant' || role === 'model') return false;
  if (role === 'sub-agent' || m.channel === 'task_report') return true;
  const t = String(m.text || '').trimStart();
  return t.startsWith('[하위 작업 완료 보고]')
    || t.startsWith('[하위 작업 중단/오류 보고]')
    || t.startsWith('[System Event] 서브 태스크');
}

function parseSubAgentReport(m) {
  const rawText = String((m && m.text) || '').trim();
  const lines = rawText.split(/\r?\n/);
  let roomTitle = '';
  let roomId = '';
  let taskId = '';
  let elapsed = '';
  let verdict = '';
  let eventType = '';
  let attempt = '';
  let statusKind = 'info';
  let statusBadge = '보고';
  let extractedReason = '';
  let collectingSummary = false;
  const summaryLines = [];
  const extraLines = [];

  for (let i = 0; i < lines.length; i++) {
    const rawLine = lines[i];
    const trimmed = rawLine.trim();

    if (i === 0 && trimmed.startsWith('[하위 작업 완료 보고]')) {
      statusKind = 'done';
      statusBadge = '완료';
      const rest = trimmed.slice('[하위 작업 완료 보고]'.length).trim();
      const bracketMatch = rest.match(/^\[([^\]]+)\]\s*(.*)$/);
      if (bracketMatch) {
        roomTitle = bracketMatch[1].trim();
        const tail = bracketMatch[2].trim();
        if (tail && tail !== '세션 작업 완료') extraLines.push(tail);
      } else if (rest && rest !== '세션 작업 완료') {
        extraLines.push(rest);
      }
      continue;
    }
    if (i === 0 && trimmed.startsWith('[하위 작업 중단/오류 보고]')) {
      statusKind = 'error';
      statusBadge = '중단/오류';
      const rest = trimmed.slice('[하위 작업 중단/오류 보고]'.length).trim();
      const bracketMatch = rest.match(/^\[([^\]]+)\]\s*(.*)$/);
      if (bracketMatch) {
        roomTitle = bracketMatch[1].trim();
        const tail = bracketMatch[2].trim();
        if (tail && tail !== '세션 작업 중단') extraLines.push(tail);
      } else if (rest && rest !== '세션 작업 중단') {
        extraLines.push(rest);
      }
      continue;
    }
    if (i === 0 && trimmed.startsWith('[System Event] 서브 태스크')) {
      const evMatch = trimmed.match(/^\[System Event\] 서브 태스크\s+(\S+?)의 보고가 도착했습니다\s*(?:\(([^,]+),\s*Attempt\s*#(\d+)\))?/);
      if (evMatch) {
        taskId = evMatch[1].trim();
        eventType = (evMatch[2] || '').trim();
        attempt = (evMatch[3] || '').trim();
        if (eventType === 'COMPLETED') {
          statusKind = 'done';
          statusBadge = '완료';
        } else if (eventType === 'FAILED' || eventType === 'PARTIAL_SUCCESS') {
          statusKind = 'error';
          statusBadge = eventType === 'FAILED' ? '실패' : '부분 완료';
        } else if (eventType) {
          statusBadge = eventType;
        }
      }
      continue;
    }

    if (trimmed.startsWith('※ 마스터 지침:')) {
      collectingSummary = false;
      continue;
    }

    const roomMatch = trimmed.match(/^[•\-]\s*대상 방:\s*(.+)$/);
    if (roomMatch) {
      collectingSummary = false;
      const val = roomMatch[1].trim();
      const idMatch = val.match(/^(.*?)\s*\(Room ID:\s*([^)]+)\)\s*$/);
      if (idMatch) {
        if (!roomTitle) roomTitle = idMatch[1].trim();
        roomId = idMatch[2].trim();
      } else if (!roomTitle) {
        roomTitle = val;
      }
      continue;
    }

    const taskMatch = trimmed.match(/^[•\-]\s*작업 ID:\s*(\S+)(?:\s*\(상태:\s*([^)]+)\))?/);
    if (taskMatch) {
      collectingSummary = false;
      taskId = taskMatch[1].trim();
      const st = (taskMatch[2] || '').trim().toLowerCase();
      if (st === 'done') {
        statusKind = 'done';
        statusBadge = '완료';
      } else if (st === 'failed' || st === 'lost' || st === 'error') {
        statusKind = 'error';
        statusBadge = '중단/오류';
      }
      continue;
    }

    const stateMatch = trimmed.match(/^[•\-]\s*상태:\s*(.+)$/);
    if (stateMatch) {
      collectingSummary = false;
      const stText = stateMatch[1].trim();
      if (stText.includes('정상 완료')) {
        statusKind = 'done';
        statusBadge = '완료';
      } else if (stText.includes('중단') || stText.includes('오류') || stText.includes('실패')) {
        statusKind = 'error';
        statusBadge = '중단/오류';
      }
      continue;
    }

    const elapsedMatch = trimmed.match(/^[•\-]\s*소요 시간:\s*(.+)$/);
    if (elapsedMatch) {
      collectingSummary = false;
      elapsed = elapsedMatch[1].trim();
      continue;
    }

    const verdictMatch = trimmed.match(/^[•\-]\s*판정\(Verdict\):\s*(.+)$/);
    if (verdictMatch) {
      collectingSummary = false;
      verdict = verdictMatch[1].trim();
      if (verdict === 'PASS') {
        statusKind = 'done';
        statusBadge = '완료';
      } else if (verdict === 'FAIL') {
        statusKind = 'error';
        statusBadge = '실패';
      }
      continue;
    }

    const summaryStartMatch = trimmed.match(/^[•\-]\s*(?:하위 에이전트 응답 요약|핵심 요약|내용):\s*(.*)$/);
    if (summaryStartMatch) {
      collectingSummary = true;
      const inlineBody = summaryStartMatch[1].trim();
      if (inlineBody && inlineBody !== '(요약 없음)') {
        summaryLines.push(inlineBody);
      }
      continue;
    }

    const sysIgnoreMatch = trimmed.match(/^[•\-]\s*(Wrapper 조치|산출물 포인터(?:\s*\([^)]*\))?|마스터 개입 필요 여부|artifact_path|raw_log_path):\s*(.*)$/);
    if (sysIgnoreMatch) {
      collectingSummary = false;
      continue;
    }

    const reasonMatch = trimmed.match(/^[•\-]\s*(?:미흡\/중단 사유|실패 사유|오류 사유|사유):\s*(.*)$/);
    if (reasonMatch) {
      collectingSummary = false;
      const reasonVal = reasonMatch[1].trim();
      if (reasonVal) {
        extractedReason = reasonVal;
        extraLines.push(`> ⚠️ **중단/오류 사유**: ${reasonVal}`);
      }
      continue;
    }

    if (collectingSummary) {
      summaryLines.push(rawLine);
    } else if (trimmed) {
      extraLines.push(rawLine);
    }
  }

  let bodyMarkdown = summaryLines.join('\n').trim();
  if (extraLines.length > 0) {
    const extraBlock = extraLines.join('\n').trim();
    bodyMarkdown = bodyMarkdown ? `${bodyMarkdown}\n\n${extraBlock}` : extraBlock;
  }
  if (!bodyMarkdown) {
    bodyMarkdown = rawText.replace(/\n*※ 마스터 지침:[^\n]*/g, '').trim() || rawText;
  }

  const metaParts = [];
  if (verdict && verdict !== '-') metaParts.push(attempt ? `${verdict} #${attempt}` : verdict);
  if (elapsed) metaParts.push(elapsed);
  if (statusBadge) metaParts.push(statusBadge);

  const tagParts = [];
  if (roomTitle) tagParts.push(roomTitle);
  else if (roomId) tagParts.push(roomId);
  if (taskId) tagParts.push(taskId);
  tagParts.push(...metaParts);

  return {
    roomTitle,
    roomId,
    taskId,
    statusKind,
    statusBadge,
    metaBadge: metaParts.join(' · ') || '보고',
    badgeLabel: tagParts.join(' · ') || '하위 작업 보고',
    badgeTitle: [roomId ? `Room ID: ${roomId}` : '', extractedReason ? `사유: ${extractedReason}` : ''].filter(Boolean).join(' · '),
    bodyMarkdown
  };
}

function buildQueueStatusTagDom(idx, orderNum) {
  const qTag = document.createElement('div');
  qTag.className = 'queue-status-tag';
  qTag.innerHTML = `
    <span class="queue-spinner"></span>
    <span>대기열 #${orderNum} · 답변 완료 후 순차 실행</span>
    <button type="button" class="queue-cancel-btn" onclick="cancelQueuedMessageByIndex(${idx})" title="대기열에서 취소">✕ 취소</button>
  `;
  return qTag;
}

function resolveDelegatedRoomInfo(raw) {
  if (!raw || typeof raw !== 'object') return null;
  let roomId = String(raw.roomId || '').trim();
  let title = String(raw.title || '').trim();
  let status = String(raw.status || 'done').toLowerCase();
  let reason = String(raw.reason || '').trim();

  // roomId 로 실제 방 매칭
  let matchedRoom = null;
  if (roomId) {
    matchedRoom = getRoomById(roomId);
  }
  // roomId 를 못 찾았고 title 이 있다면 chatRooms 에서 제목 일치 검색
  if (!matchedRoom && title && Array.isArray(chatRooms)) {
    matchedRoom = chatRooms.find(r => !ORCHESTRATOR_ROOM_IDS.includes(r.id) && (r.title === title || r.id === title));
    if (matchedRoom) {
      roomId = matchedRoom.id;
    }
  }

  if (matchedRoom) {
    if (!title || title === roomId) title = matchedRoom.title || roomId;
    if (!roomId) roomId = matchedRoom.id;
  }

  if (!roomId && !title) return null;
  const finalRoomId = roomId || title;
  if (ORCHESTRATOR_ROOM_IDS.includes(finalRoomId)) return null;

  // 실시간 실행 상태(running/done/error) 판정
  const live = roomLiveStates[finalRoomId];
  if (live && live.isRunning) {
    status = 'running';
    if (!reason || reason === '정상 완료') reason = live.feed || '백그라운드 실행 중...';
  } else if (typeof serverStatus !== 'undefined' && serverStatus && Array.isArray(serverStatus.activeRuns) && serverStatus.activeRuns.some(r => r.roomId === finalRoomId)) {
    status = 'running';
    if (!reason || reason === '정상 완료') reason = '백그라운드 실행 중...';
  }

  if (!reason) {
    if (status === 'running') reason = '백그라운드 실행 중';
    else if (status === 'error') reason = '명령 또는 세션 오류';
    else reason = '정상 완료';
  }

  return {
    roomId: finalRoomId,
    title: title || finalRoomId,
    status: status,
    reason: reason
  };
}

function extractDelegatedRoomsFromSteps(steps) {
  if (!Array.isArray(steps) || steps.length === 0) return [];
  const results = [];
  const seenRoomIds = new Set();

  for (let i = 0; i < steps.length; i++) {
    const s = steps[i];
    if (!s || typeof s !== 'object') continue;
    const title = String(s.title || '');
    const detail = String(s.detail || '');
    const output = String(s.output || '');

    let nextResultText = '';
    for (let j = i + 1; j < Math.min(i + 3, steps.length); j++) {
      const nextS = steps[j];
      if (nextS && nextS.kind === 'tool_result') {
        nextResultText += '\n' + String(nextS.detail || nextS.output || '');
      }
    }
    const combined = `${title}\n${detail}\n${output}\n${nextResultText}`;

    const isSpawn = combined.includes('session-ctl spawn') || title.startsWith('session-ctl spawn');
    const isSend = combined.includes('session-ctl send') || title.startsWith('session-ctl send');
    if (!isSpawn && !isSend) continue;

    let extractedRoomId = '';
    let extractedTitle = '';

    // 패턴 1: [방제목] ((Room )?ID: room-...)
    const bracketMatch = combined.match(/\[([^\]]+)\]\s*\((?:Room\s+)?ID:\s*(room-[a-zA-Z0-9_\-]+)/i);
    if (bracketMatch) {
      extractedTitle = bracketMatch[1].trim();
      extractedRoomId = bracketMatch[2].trim();
    }

    // 패턴 2: Room ID: room-...
    if (!extractedRoomId) {
      const ridMatch = combined.match(/(?:Room\s+ID|roomId|room_id)["']?\s*[:=]\s*["']?(room-[a-zA-Z0-9_\-]+)/i);
      if (ridMatch) extractedRoomId = ridMatch[1].trim();
    }

    // 패턴 3: JSON 모드 {"roomId": "...", "title": "..."}
    if (!extractedTitle) {
      const jTitleMatch = combined.match(/"title"\s*:\s*"([^"]+)"/);
      if (jTitleMatch) extractedTitle = jTitleMatch[1].trim();
    }

    // 패턴 4: 명령어에서 title / target 추출
    if (isSpawn && !extractedTitle) {
      const spawnMatch = combined.match(/session-ctl\s+spawn\s+(?:--[a-zA-Z0-9_\-]+(?:\s+[^\s"'\-]+)?\s+)*(?:["']([^"']+)["']|(\S+))/);
      if (spawnMatch) extractedTitle = (spawnMatch[1] || spawnMatch[2] || '').trim();
      else if (title.includes(':') && title.includes('spawn')) extractedTitle = title.split(':', 2)[1].trim();
    }

    if (isSend) {
      const sendMatch = combined.match(/session-ctl\s+send\s+(?:--[a-zA-Z0-9_\-]+(?:\s+[^\s"'\-]+)?\s+)*(?:["']([^"']+)["']|(\S+))/);
      if (sendMatch) {
        const tgt = (sendMatch[1] || sendMatch[2] || '').trim();
        if (tgt.startsWith('room-')) {
          if (!extractedRoomId) extractedRoomId = tgt;
        } else if (!extractedTitle) {
          extractedTitle = tgt;
        }
      } else if (title.includes(':') && title.includes('send')) {
        const tgt = title.split(':', 2)[1].trim();
        if (tgt.startsWith('room-')) {
          if (!extractedRoomId) extractedRoomId = tgt;
        } else if (!extractedTitle) {
          extractedTitle = tgt;
        }
      }
    }

    // 도구 실행 자체(spawn/send 명령) 결과에서 에러 검사
    // 注意: 프롬프트 본문(detail)에 '오류' 단어가 포함된 것만으로 실패 처리하지 않는다!
    const resOutput = `${output}\n${nextResultText}`.trim();
    let cmdFailed = false;
    let cmdReason = '';

    if (s.status === 'error') {
      cmdFailed = true;
      cmdReason = String(s.error || s.detail || '도구 실행 실패');
    } else if (resOutput) {
      const codeMatch = resOutput.match(/Command exited with code\s+([1-9]\d*)/);
      if (codeMatch) {
        cmdFailed = true;
        const errOutMatch = resOutput.match(/Command output:\s*([\s\S]*)/);
        cmdReason = errOutMatch ? errOutMatch[1].trim().split('\n')[0] : `명령어 비정상 종료 (코드 ${codeMatch[1]})`;
      } else if (resOutput.includes('API 연결 실패') || resOutput.includes('세션 생성 실패') || resOutput.includes('세션 지시 실패')) {
        cmdFailed = true;
        cmdReason = resOutput.split('\n')[0].replace(/^⚠️\s*/, '');
      }
    }

    const initialStatus = cmdFailed ? 'error' : 'done';
    const initialReason = cmdFailed ? (cmdReason || '명령 실행 오류') : '정상 완료';

    const item = resolveDelegatedRoomInfo({
      roomId: extractedRoomId,
      title: extractedTitle,
      status: initialStatus,
      reason: initialReason
    });

    if (item && item.roomId && !seenRoomIds.has(item.roomId) && !ORCHESTRATOR_ROOM_IDS.includes(item.roomId)) {
      seenRoomIds.add(item.roomId);
      results.push(item);
    }
  }

  return results;
}

function extractDelegatedRoomsFromMessage(m) {
  if (!m || typeof m !== 'object') return [];
  if (Array.isArray(m.delegatedRooms) && m.delegatedRooms.length > 0) {
    return m.delegatedRooms.map(item => resolveDelegatedRoomInfo(item)).filter(Boolean);
  }
  if (Array.isArray(m.delegated_rooms) && m.delegated_rooms.length > 0) {
    return m.delegated_rooms.map(item => resolveDelegatedRoomInfo(item)).filter(Boolean);
  }
  return extractDelegatedRoomsFromSteps(m.steps);
}

function buildDelegatedSubAgentChipsDom(delegatedRooms, parentRoomId = null) {
  if (!Array.isArray(delegatedRooms) || delegatedRooms.length === 0) return null;

  const pId = String(parentRoomId || currentRoomId || '').trim();
  let chipIcon = ICONS.spark;
  if (pId === EXPERT_VAULT_ROOM_ID) {
    chipIcon = ICONS.brain;
  } else if (pId === EXPERT_CODING_ROOM_ID) {
    chipIcon = ICONS.terminal;
  }

  const wrap = document.createElement('div');
  wrap.className = 'sub-agent-chips-wrap';

  const listEl = document.createElement('div');
  listEl.className = 'sub-agent-chips-list';

  delegatedRooms.forEach(item => {
    if (!item || !item.roomId) return;
    const chipBtn = document.createElement('button');
    chipBtn.type = 'button';
    chipBtn.className = `sub-agent-chip is-${item.status || 'done'}`;
    const statusLabel = item.status === 'running' ? '실행 중' : (item.status === 'error' ? '오류' : '완료');
    const reasonText = item.reason || statusLabel;
    chipBtn.title = `[${item.title || item.roomId}] · 상태: ${statusLabel} (${reasonText})\n클릭 시 해당 채팅방으로 이동`;
    chipBtn.setAttribute('data-room-id', item.roomId);

    let badgeHtml = '';
    if (item.status === 'running') {
      badgeHtml = `<span class="chip-badge status-running" title="${escapeHtml(reasonText)}"><span class="chip-dot"></span>실행 중</span>`;
    } else if (item.status === 'error') {
      const shortReason = reasonText.length > 18 ? reasonText.slice(0, 18) + '…' : reasonText;
      const reasonSuffix = reasonText && reasonText !== '오류' && reasonText !== '오류 발생'
        ? ` · <span class="chip-badge-reason">${escapeHtml(shortReason)}</span>`
        : '';
      badgeHtml = `<span class="chip-badge status-error" title="오류 사유: ${escapeHtml(reasonText)}">오류${reasonSuffix}</span>`;
    } else {
      badgeHtml = `<span class="chip-badge status-done" title="정상 완료">완료</span>`;
    }

    chipBtn.innerHTML = `
      <span class="chip-icon">${chipIcon}</span>
      <span class="chip-title">${escapeHtml(item.title || item.roomId)}</span>
      ${badgeHtml}
      <span class="chip-arrow">${ICONS.chevRight}</span>
    `;

    chipBtn.addEventListener('click', (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      selectRoom(item.roomId);
    });

    listEl.appendChild(chipBtn);
  });

  wrap.appendChild(listEl);
  return wrap;
}

/**
 * 말풍선 좌측 어시스턴트 발화자 표시 정보(역할/이름/아바타/뱃지)를 결정한다.
 * 마스터, 세컨브레인, 봇개발 전문가 및 하위 에이전트의 실제 역할/이름을 매핑한다.
 */
function getAssistantDisplayInfo(targetRoom = null, m = null) {
  const room = targetRoom || (m && m.roomId ? getRoomById(m.roomId) : getCurrentRoom());
  const rId = String((room && (room.id || room.roomId)) || currentRoomId || '').trim();

  // 1. 메시지 레벨에 명시적 에이전트 이름/역할이 있는 경우 최우선 존중
  if (m && (m.agentName || m.senderName || m.agentRole)) {
    const explicitName = String(m.agentName || m.senderName || m.agentRole).trim();
    if (explicitName) {
      return {
        name: explicitName,
        fullName: explicitName,
        role: 'custom',
        avatarCls: 'is-subtask',
        avatarIcon: ICONS.spark,
        roleBadgeHtml: ''
      };
    }
  }

  // 2. 마스터 오케스트레이터 에이전트 (__core__)
  if (rId === CORE_ROOM_ID) {
    return {
      name: '마스터 에이전트',
      fullName: '마스터 에이전트 (Core Orchestrator)',
      role: 'master',
      avatarCls: 'is-master',
      avatarIcon: ICONS.spark,
      roleBadgeHtml: ''
    };
  }

  // 3. 세컨브레인 전문가 에이전트 (__expert_vault__)
  if (rId === EXPERT_VAULT_ROOM_ID) {
    return {
      name: '세컨브레인 전문가',
      fullName: '세컨브레인 전문가 (Obsidian Vault & Life Domains)',
      role: 'expert_vault',
      avatarCls: 'is-vault',
      avatarIcon: ICONS.brain,
      roleBadgeHtml: ''
    };
  }

  // 4. 봇개발 전문가 에이전트 (__expert_coding__)
  if (rId === EXPERT_CODING_ROOM_ID) {
    return {
      name: '봇개발 전문가',
      fullName: '봇개발 전문가 (oshyun-bot Coding & Tests)',
      role: 'expert_coding',
      avatarCls: 'is-coding',
      avatarIcon: ICONS.terminal,
      roleBadgeHtml: ''
    };
  }

  // 4-1. 사용자 정의 커스텀 에이전트 (__custom_<slug>__)
  const customCfg = getAgentConfigById(rId);
  if (customCfg && !customCfg.builtin) {
    return {
      name: customCfg.name || rId,
      fullName: `${customCfg.name || rId}${customCfg.roleDescription ? ` (${customCfg.roleDescription})` : ''}`,
      role: 'custom_agent',
      avatarCls: 'is-subtask',
      avatarIcon: `<span>${escapeHtml(customCfg.emoji || '🤖')}</span>`,
      roleBadgeHtml: ''
    };
  }

  // 5. 하위 세션 (마스터 / 전문가 / 커스텀 직속 하위 에이전트)
  const isSubtask = Boolean((room && isMasterSubtaskRoom(room)) || (room && room.parentAgentId) || (m && m.parentAgentId));
  if (isSubtask) {
    let pId = String((room && room.parentAgentId) || (m && m.parentAgentId) || '').trim();
    if (!pId && Array.isArray(latestMasterTasks)) {
      const t = latestMasterTasks.find(task => task && (task.roomId === rId || (room && task.roomId === room.id)));
      if (t && t.parentAgentId) pId = String(t.parentAgentId).trim();
    }
    if (!pId && latestMasterGraph && Array.isArray(latestMasterGraph.nodes)) {
      const node = latestMasterGraph.nodes.find(n => n && n.id === rId);
      if (node && node.parentAgentId) pId = String(node.parentAgentId).trim();
    }

    let workerName = '하위 일꾼';
    let avatarIcon = ICONS.spark;
    let avatarCls = 'is-subtask';

    const parentCustomCfg = getAgentConfigById(pId);
    if (pId === EXPERT_VAULT_ROOM_ID) {
      workerName = '세컨브레인 일꾼';
      avatarIcon = ICONS.brain;
      avatarCls = 'is-vault';
    } else if (pId === EXPERT_CODING_ROOM_ID) {
      workerName = '봇개발 일꾼';
      avatarIcon = ICONS.terminal;
      avatarCls = 'is-coding';
    } else if (pId === CORE_ROOM_ID) {
      workerName = '마스터 일꾼';
      avatarIcon = ICONS.spark;
      avatarCls = 'is-subtask';
    } else if (parentCustomCfg) {
      workerName = `${parentCustomCfg.name || pId} 일꾼`;
      avatarIcon = `<span>${escapeHtml(parentCustomCfg.emoji || '🤖')}</span>`;
      avatarCls = 'is-subtask';
    }

    // 연결된 원장 태스크 조회 (있으면 작업 제목 확인 - 마우스 hover 툴팁용)
    const linkedTask = Array.isArray(latestMasterTasks)
      ? latestMasterTasks.find(t => t && (t.roomId === rId || (room && t.roomId === room.id)))
      : null;

    const rawTitle = String(
      (linkedTask && linkedTask.title) ||
      (room && room.title) ||
      ''
    ).trim();

    const isGeneric = !rawTitle ||
      rawTitle === '새 대화' ||
      rawTitle === '새 세션' ||
      rawTitle === '에이전트 세션' ||
      rawTitle === rId;

    // fullName은 발화자명 마우스 오버 시 상세 툴팁으로 사용됨
    const fullTooltip = (!isGeneric && rawTitle) ? `${workerName} (${rawTitle})` : workerName;

    return {
      name: workerName,
      fullName: fullTooltip,
      role: 'subtask',
      avatarCls: avatarCls,
      avatarIcon: avatarIcon,
      roleBadgeHtml: ''
    };
  }

  // 6. 일반 대화방 (사용자 개별 대화)
  return {
    name: '에이전트',
    fullName: '에이전트',
    role: 'general',
    avatarCls: '',
    avatarIcon: ICONS.spark,
    roleBadgeHtml: ''
  };
}

function buildMessageRowDom(m, idx, qOrder = 0) {
  if (m.isHtml && typeof m.text === 'string' && m.text.includes('welcome-box')) return null;
  const sender = m.sender || m.role || 'assistant';
  const isSubAgent = isSubAgentReportMessage(m);
  const isQueued = Boolean((sender === 'user' || isSubAgent) && m.queued);
  const row = document.createElement('div');
  row.className = 'msg ' + (isSubAgent ? 'assistant sub-agent' : sender)
    + (isQueued ? ' queued' : '')
    + (!isSubAgent && (m.isError || (typeof m.text === 'string' && m.text.startsWith('⚠️'))) ? ' error' : '');
  row.setAttribute('data-msg-idx', String(idx));

  if (isSubAgent) {
    const parsed = parseSubAgentReport(m);
    if (parsed.statusKind === 'error') {
      row.classList.add('is-error');
    }
    if (isQueued) {
      row.appendChild(buildQueueStatusTagDom(idx, qOrder || m.queuePos || 1));
    }

    const head = document.createElement('div');
    head.className = 'assistant-head';
    const avatarErrCls = parsed.statusKind === 'error' ? ' is-error' : '';
    const tagTitleAttr = parsed.badgeTitle ? ` title="${escapeHtml(parsed.badgeTitle)}"` : '';
    const matchedSubRoom = parsed.roomId ? getRoomById(parsed.roomId) : (parsed.roomTitle ? findChatRoom(parsed.roomTitle) : null);
    const navSubTarget = (matchedSubRoom && matchedSubRoom.id) || parsed.roomId || (matchedSubRoom ? matchedSubRoom.title : (findChatRoom(parsed.roomTitle) ? parsed.roomTitle : ''));
    const roomChipHtml = navSubTarget
      ? `<button type="button" class="sub-agent-room-btn" onclick="navigateToChatRoom('${escapeHtml(navSubTarget)}')" title="해당 세션으로 이동">${escapeHtml(parsed.roomTitle || (matchedSubRoom && matchedSubRoom.title) || navSubTarget)}</button>`
      : `<span style="font-weight:600;">${escapeHtml(parsed.roomTitle || '하위 에이전트')}</span>`;
    head.innerHTML = `<span class="assistant-avatar sub-agent-avatar${avatarErrCls}">${ICONS.spark}</span>${roomChipHtml}<span class="model-tag sub-agent-tag is-${parsed.statusKind}"${tagTitleAttr}>${escapeHtml(parsed.metaBadge)}</span>${buildMsgTimeHtml(m.createdAt)}`;
    row.appendChild(head);

    const md = document.createElement('div');
    md.className = 'md sub-agent-md';
    const bodyLines = parsed.bodyMarkdown.split(/\r?\n/).filter(l => l.trim().length > 0);
    if (bodyLines.length > 4 || parsed.bodyMarkdown.length > 260) {
      const previewText = bodyLines.slice(0, 2).join('\n');
      const shortPreview = previewText.length > 200 ? `${previewText.slice(0, 200)}…` : previewText;
      const previewDiv = document.createElement('div');
      previewDiv.className = 'sub-agent-preview';
      previewDiv.innerHTML = formatMarkdown(shortPreview);
      md.appendChild(previewDiv);

      const details = document.createElement('details');
      details.className = 'sub-agent-details';
      const summaryEl = document.createElement('summary');
      const detailsBody = document.createElement('div');
      detailsBody.className = 'sub-agent-details-body';
      detailsBody.innerHTML = formatMarkdown(parsed.bodyMarkdown);

      const isExpanded = Boolean(m._expanded);
      if (isExpanded) {
        details.open = true;
        md.classList.add('is-expanded');
        previewDiv.style.display = 'none';
        summaryEl.textContent = '전체 보고 접기 ▴';
      } else {
        details.open = false;
        md.classList.remove('is-expanded');
        previewDiv.style.display = '';
        summaryEl.textContent = '전체 보고 보기 ▾';
      }

      details.addEventListener('toggle', () => {
        const isOpen = details.open;
        m._expanded = isOpen;
        md.classList.toggle('is-expanded', isOpen);
        previewDiv.style.display = isOpen ? 'none' : '';
        summaryEl.textContent = isOpen ? '전체 보고 접기 ▴' : '전체 보고 보기 ▾';
      });

      details.appendChild(summaryEl);
      details.appendChild(detailsBody);
      md.appendChild(details);
    } else {
      md.innerHTML = formatMarkdown(parsed.bodyMarkdown);
    }
    row.appendChild(md);

    const actions = document.createElement('div');
    actions.className = 'msg-actions';
    actions.innerHTML = `
      <button class="icon-btn" onclick="copyMessageByIndex(${idx})" title="보고 복사">${ICONS.copy}</button>
      <button class="icon-btn" onclick="speakMessageByIndex(${idx})" title="음성으로 듣기">${ICONS.volume}</button>
    `;
    row.appendChild(actions);
  } else if (sender === 'user') {
    if (m.channel === 'orchestrator') {
      const curRoom = (m && m.roomId ? getRoomById(m.roomId) : getCurrentRoom());
      let pId = String((curRoom && curRoom.parentAgentId) || (m && m.parentAgentId) || '').trim();
      if (!pId && curRoom && Array.isArray(latestMasterTasks)) {
        const t = latestMasterTasks.find(task => task && task.roomId === curRoom.id);
        if (t && t.parentAgentId) pId = String(t.parentAgentId).trim();
      }
      let orchLabel = '마스터 에이전트 지시';
      let orchCls = '';
      const parentCfg = getAgentConfigById(pId);
      if (pId === EXPERT_CODING_ROOM_ID) {
        orchLabel = '봇개발 전문가 지시';
        orchCls = ' is-coding';
      } else if (pId === EXPERT_VAULT_ROOM_ID) {
        orchLabel = '세컨브레인 전문가 지시';
        orchCls = ' is-vault';
      } else if (parentCfg && !parentCfg.builtin) {
        orchLabel = `${parentCfg.name || pId} 지시`;
      }
      const orchTag = document.createElement('div');
      orchTag.className = 'orch-msg-tag' + orchCls;
      orchTag.innerHTML = `<span>${escapeHtml(orchLabel)}</span>`;
      row.appendChild(orchTag);
    }
    if (isQueued) {
      row.appendChild(buildQueueStatusTagDom(idx, qOrder || m.queuePos || 1));
    }

    if (m.attachments && m.attachments.length > 0) {
      const attWrap = document.createElement('div');
      attWrap.className = 'msg-attachments';
      attWrap.innerHTML = m.attachments.map(att => {
        const fileUrl = att.url || (att.savedName ? `/api/attachments/${att.savedName}` : '');
        const isImg = (att.type && att.type.startsWith('image/')) || (fileUrl && fileUrl.match(/\.(png|jpe?g|gif|webp|bmp|svg)$/i));
        if (isImg && fileUrl) {
          return `
            <div class="msg-attachment-item" onclick="openImageLightbox('${escapeHtml(fileUrl)}')" title="${escapeHtml(att.name || '이미지')}">
              <img src="${escapeHtml(fileUrl)}" alt="${escapeHtml(att.name || '이미지')}" loading="lazy">
            </div>
          `;
        }
        return `
          <a class="msg-attachment-item" href="${escapeHtml(fileUrl)}" target="_blank" style="padding:8px 12px;font-size:13px;color:var(--accent);text-decoration:none;">
            📎 ${escapeHtml(att.name || '첨부파일')}
          </a>
        `;
      }).join('');
      row.appendChild(attWrap);
    }

    if (m.text || !(m.attachments && m.attachments.length > 0)) {
      const msgText = m.text || '';
      const lineCount = msgText ? msgText.split('\n').length : 0;
      const isLongBubble = msgText.length > 260 || lineCount > 6;
      const b = document.createElement('div');
      if (!isLongBubble) {
        b.className = 'bubble';
        b.textContent = msgText;
      } else {
        const expanded = Boolean(m._expanded);
        b.className = `bubble is-collapsible ${expanded ? 'is-expanded' : 'is-collapsed'}`;
        const bodyEl = document.createElement('div');
        bodyEl.className = 'bubble-text-body';
        bodyEl.textContent = msgText;
        const summaryLabel = `전체 보기 (${msgText.length.toLocaleString()}자 · ${lineCount.toLocaleString()}줄) ▾`;

        const topToggleBtn = document.createElement('button');
        topToggleBtn.type = 'button';
        topToggleBtn.className = 'bubble-expand-btn is-top';
        topToggleBtn.textContent = '접기 ▴';

        const bottomToggleBtn = document.createElement('button');
        bottomToggleBtn.type = 'button';
        bottomToggleBtn.className = 'bubble-expand-btn is-bottom';
        bottomToggleBtn.textContent = expanded ? '접기 ▴' : summaryLabel;

        const setBubbleExpanded = (nextExpanded) => {
          m._expanded = nextExpanded;
          b.classList.toggle('is-expanded', nextExpanded);
          b.classList.toggle('is-collapsed', !nextExpanded);
          bottomToggleBtn.textContent = nextExpanded ? '접기 ▴' : summaryLabel;
        };

        const onToggleClick = (ev) => {
          ev.stopPropagation();
          setBubbleExpanded(!m._expanded);
        };
        topToggleBtn.addEventListener('click', onToggleClick);
        bottomToggleBtn.addEventListener('click', onToggleClick);
        b.addEventListener('click', () => {
          if (!m._expanded) {
            setBubbleExpanded(true);
          }
        });

        b.appendChild(topToggleBtn);
        b.appendChild(bodyEl);
        b.appendChild(bottomToggleBtn);
      }
      row.appendChild(b);
    }

    const uFooter = document.createElement('div');
    uFooter.className = 'msg-user-footer';
    const uActions = document.createElement('div');
    uActions.className = 'msg-actions';
    uActions.innerHTML = `
      <button class="icon-btn" onclick="copyMessageByIndex(${idx})" title="질문 복사">${ICONS.copy}</button>
      <button class="icon-btn" onclick="editMessageToInputByIndex(${idx})" title="입력창으로 가져와 수정">${ICONS.edit}</button>
      ${isQueued
        ? `<button class="icon-btn" onclick="cancelQueuedMessageByIndex(${idx})" title="대기 취소">${ICONS.close}</button>`
        : `<button class="icon-btn" onclick="retryMessageByIndex(${idx})" title="다시 질문하기">${ICONS.refresh}</button>`}
    `;
    uFooter.appendChild(uActions);
    const uTimeHtml = buildMsgTimeHtml(m.createdAt);
    if (uTimeHtml) {
      uFooter.insertAdjacentHTML('beforeend', uTimeHtml);
    }
    row.appendChild(uFooter);
  } else {
    const head = document.createElement('div');
    head.className = 'assistant-head';
    const headEffort = m.effort || (typeof serverStatus !== 'undefined' && serverStatus && serverStatus.effort ? serverStatus.effort : '');
    const modelLabel = m.model ? `<span class="model-tag">${escapeHtml(m.model + (headEffort ? ` (${headEffort})` : ''))}</span>` : '';
    const agentInfo = getAssistantDisplayInfo(getCurrentRoom(), m);
    head.innerHTML = `<span class="assistant-avatar ${agentInfo.avatarCls}">${agentInfo.avatarIcon}</span><span class="assistant-name" title="${escapeHtml(agentInfo.fullName)}">${escapeHtml(agentInfo.name)}</span>${agentInfo.roleBadgeHtml}${modelLabel}${buildMsgTimeHtml(m.createdAt)}`;
    row.appendChild(head);

    const toolSteps = filterToolSteps(m.steps);
    if (toolSteps.length > 0) {
      const thinkingCount = toolSteps.filter(s => s && s.kind === 'thinking').length;
      const toolCount = toolSteps.length - thinkingCount;
      let labelText = `${toolSteps.length} tools`;
      if (thinkingCount > 0 && toolCount === 0) {
        labelText = 'thinking';
      } else if (thinkingCount > 0 && toolCount > 0) {
        labelText = `thinking + ${toolCount} tools`;
      }
      const stepsIconSvg = (thinkingCount > 0 && toolCount === 0) ? (ICONS.brain || ICONS.spark) : ICONS.tool;
      const stepsDetails = document.createElement('details');
      stepsDetails.className = 'steps';
      stepsDetails.innerHTML = `
        <summary><span class="steps-icon">${stepsIconSvg}</span>${ICONS.chevRight}<span>${labelText}</span></summary>
        <div class="steps-list">${toolSteps.map(s => `<details class="step">${buildStepItemHtml(s, true)}</details>`).join('')}</div>
      `;
      row.appendChild(stepsDetails);
    }

    let displayBody = (m.text || '').trim();
    const isToolOnly = (displayBody === '(도구 실행 완료)' || displayBody === '(도구 실행이 완료되었습니다)');
    if (isToolOnly) {
      displayBody = '';
    }

    const isErrorMsg = Boolean(m.isError || (typeof m.text === 'string' && (m.text.startsWith('⚠️') || m.text.startsWith('🛑'))));

    if (displayBody && !isErrorMsg) {
      const md = document.createElement('div');
      md.className = 'md';
      md.innerHTML = m.isHtml ? displayBody : formatMarkdown(displayBody);
      row.appendChild(md);
    } else if (isErrorMsg) {
      const errCard = document.createElement('div');
      errCard.className = 'msg-error-card';
      let errCategory = '실행 오류';
      if (displayBody.includes('서버 재시작')) errCategory = '서버 재시작';
      else if (displayBody.includes('중단')) errCategory = '작업 중단';
      else if (displayBody.includes('기동 실패')) errCategory = '에이전트 기동 실패';
      else if (displayBody.includes('세션')) errCategory = '세션 만료/오류';
      else if (displayBody.includes('취소')) errCategory = '사용자 취소';

      const lastStep = toolSteps && toolSteps.length > 0 ? toolSteps[toolSteps.length - 1] : null;
      let stepDiag = '';
      if (lastStep) {
        stepDiag = `마지막 도구 단계: [${lastStep.tool || lastStep.kind || 'tool'}] ${lastStep.title || ''}\n상태: ${lastStep.status || 'unknown'}`;
        if (lastStep.detail) stepDiag += `\n상세: ${String(lastStep.detail).slice(0, 300)}`;
      }

      errCard.innerHTML = `
        <div class="msg-card-head">
          <span class="card-icon">⚠️</span>
          <span>작업 오류 및 중단 안내</span>
          <span class="card-badge">${escapeHtml(errCategory)}</span>
        </div>
        <div class="msg-card-desc">${displayBody ? formatMarkdown(displayBody) : '작업 실행 중 예외가 발생하거나 비정상적으로 중단되었습니다.'}</div>
        <details class="msg-card-details">
          <summary>🔍 상세 원인 및 시스템 진단 보기</summary>
          <pre>${escapeHtml([
            `• 원인 분류: ${errCategory}`,
            m.session_id || m.sessionId ? `• 세션 ID: ${m.session_id || m.sessionId}` : '',
            m.model ? `• 사용 모델: ${m.model}` : '',
            stepDiag ? `• ${stepDiag}` : '',
            `• 조치 안내: 네트워크/로그 확인 후 '다시 질문하기' 버튼을 누르거나 ➕ 새 대화로 시작해 보세요.`
          ].filter(Boolean).join('\n'))}</pre>
        </details>
      `;
      row.appendChild(errCard);
    } else if (!displayBody) {
      const emptyCard = document.createElement('div');
      emptyCard.className = 'msg-empty-card';
      const hasTools = toolSteps && toolSteps.length > 0;
      emptyCard.innerHTML = `
        <div class="msg-card-head">
          <span class="card-icon">⚠️</span>
          <span>${hasTools ? '답변 텍스트 미작성' : '응답 없음 (빈 응답)'}</span>
          <span class="card-badge">${hasTools ? '도구 실행만 완료됨' : '출력 누락'}</span>
        </div>
        <div class="msg-card-desc">
          ${hasTools
            ? '도구 실행은 모두 완료되었으나 모델이 최종 응답 텍스트를 작성하지 않고 종료했습니다. 위 도구 단계 카드를 펼쳐 실행 결과를 확인하실 수 있습니다.'
            : '모델 프로세스는 정상 종료되었으나 반환된 텍스트 응답이 비어 있습니다.'}
        </div>
        <details class="msg-card-details">
          <summary>🔍 원인 분석 및 재시도 안내</summary>
          <pre>${escapeHtml([
            `• 사유: ${hasTools ? '모델이 도구 호출 후 최종 응답을 출력하지 않고 종료됨' : '세션 응답 누락 또는 프로세스 조기 종료'}`,
            m.session_id || m.sessionId ? `• 세션 ID: ${m.session_id || m.sessionId}` : '',
            m.model ? `• 사용 모델: ${m.model}` : '',
            `• 해결 방법: 하단 '다시 질문하기'를 클릭해 재전송하거나, ➕ 새 대화를 생성하여 질문해 보세요.`
          ].filter(Boolean).join('\n'))}</pre>
        </details>
      `;
      row.appendChild(emptyCard);
    }

    if (ORCHESTRATOR_ROOM_IDS.includes(currentRoomId)) {
      const delegatedRooms = extractDelegatedRoomsFromMessage(m);
      if (delegatedRooms && delegatedRooms.length > 0) {
        const chipsDom = buildDelegatedSubAgentChipsDom(delegatedRooms, currentRoomId);
        if (chipsDom) row.appendChild(chipsDom);
      }
    }

    const actions = document.createElement('div');
    actions.className = 'msg-actions';
    const metaParts = [];
    if (m.model) {
      const effortVal = m.effort || (typeof serverStatus !== 'undefined' && serverStatus && serverStatus.effort ? serverStatus.effort : '');
      metaParts.push(effortVal ? `${m.model} (${effortVal})` : m.model);
    }
    if (m.elapsedSeconds) metaParts.push(`${m.elapsedSeconds}s`);
    actions.innerHTML = `
      <button class="icon-btn" onclick="copyMessageByIndex(${idx})" title="답변 복사">${ICONS.copy}</button>
      <button class="icon-btn" onclick="speakMessageByIndex(${idx})" title="음성으로 듣기">${ICONS.volume}</button>
      <button class="icon-btn" onclick="retryMessageByIndex(${idx})" title="다시 생성">${ICONS.refresh}</button>
      ${metaParts.length ? `<span class="msg-meta">${escapeHtml(metaParts.join(' · '))}</span>` : ''}
    `;
    row.appendChild(actions);
  }

  return row;
}

function renderMessagesForCurrentRoom(forceScroll = false) {
  const container = document.getElementById('messages-container');
  const room = getCurrentRoom();
  if (!container || !room) return;

  const msgs = room.messages || [];
  const liveState = ensureRoomLiveState(room.id);

  if (msgs.length === 0 && !liveState.isRunning) {
    const isCore = room.id === CORE_ROOM_ID;
    const isVault = room.id === EXPERT_VAULT_ROOM_ID;
    const isCoding = room.id === EXPERT_CODING_ROOM_ID;
    const items = isCore
      ? CORE_ORCHESTRATOR_SUGGESTIONS
      : DEFAULT_SUGGESTIONS.slice(0, 4);
    const cards = items.map((it, idx) => `
      <button class="suggestion" onclick="onSuggestionClick(${idx})">
        <div class="s-tag">${escapeHtml(it.tag || '추천')}</div>
        <div class="s-title">${escapeHtml(it.title)}</div>
      </button>
    `).join('');
    let emptyTitle = '무엇을 도와드릴까요?';
    let emptyDesc = '옵시디언 볼트의 노트·일정·할 일을 읽고 답변합니다';
    const roomAgentCfg = getAgentConfigById(room.id);
    if (isCore) {
      emptyTitle = (roomAgentCfg && roomAgentCfg.name) || '마스터 에이전트';
      emptyDesc = (roomAgentCfg && roomAgentCfg.roleDescription) || '모든 채팅방(AGY 세션)의 대화를 열람·요약하고 특정 세션에 작업을 지시하거나 새 세션을 생성합니다';
    } else if (isVault) {
      emptyTitle = (roomAgentCfg && roomAgentCfg.name) || '세컨브레인 에이전트';
      emptyDesc = (roomAgentCfg && roomAgentCfg.roleDescription) || 'Obsidian 볼트 지식·5대 도메인(건강, 할일, 식재료, 금융, 차량) 동기화를 전담합니다';
    } else if (isCoding) {
      emptyTitle = (roomAgentCfg && roomAgentCfg.name) || '봇개발 에이전트';
      emptyDesc = (roomAgentCfg && roomAgentCfg.roleDescription) || 'oshyun-bot 소스코드 개발·단위 테스트·토큰버닝·리팩토링을 전담합니다';
    } else if (roomAgentCfg) {
      emptyTitle = `${roomAgentCfg.emoji || '🤖'} ${roomAgentCfg.name || room.id}`;
      emptyDesc = roomAgentCfg.roleDescription || '사용자 정의 전문 에이전트입니다';
    }
    container.innerHTML = `
      <div class="empty-state">
        <div class="empty-logo">${ICONS.spark}</div>
        <h2>${escapeHtml(emptyTitle)}</h2>
        <p>${escapeHtml(emptyDesc)}</p>
        <div class="suggestions">${cards}</div>
      </div>
    `;
    updateScrollBottomButton();
    return;
  }

  const thread = document.createElement('div');
  thread.className = 'thread';

  const activeEntries = [];
  const queuedEntries = [];
  msgs.forEach((m, idx) => {
    if (!m) return;
    const sender = m.sender || m.role || 'assistant';
    if ((sender === 'user' || isSubAgentReportMessage(m)) && m.queued) {
      queuedEntries.push({ m, idx });
    } else {
      activeEntries.push({ m, idx });
    }
  });

  // 1. 현재 진행 중이거나 완료된 메시지 먼저 렌더링
  // 단, 현재 liveState.isRunning 중인데 마지막 메시지가 텍스트 없는 어시스턴트 플레이스홀더라면
  // liveRow 와 중복되므로 렌더링에서 제외 (새로고침 시 2개 찍힘 방지)
  if (liveState.isRunning && activeEntries.length > 0) {
    const lastEntry = activeEntries[activeEntries.length - 1];
    const lastSender = lastEntry.m.sender || lastEntry.m.role || '';
    if (lastSender === 'assistant') {
      const isPlaceholder = !String(lastEntry.m.text || '').trim();
      const isMatchingAsst = Boolean(liveState.asstMsgId && lastEntry.m.id === liveState.asstMsgId);
      if (isPlaceholder || isMatchingAsst) {
        activeEntries.pop();
      }
    }
  }

  let lastDateKey = '';
  const appendDateDividerIfNeeded = (ts) => {
    const dk = getMessageDateKey(ts);
    if (dk && dk !== lastDateKey) {
      lastDateKey = dk;
      const label = formatDateDividerLabel(ts);
      if (label) {
        const div = document.createElement('div');
        div.className = 'chat-date-divider';
        div.innerHTML = `<span>${escapeHtml(label)}</span>`;
        thread.appendChild(div);
      }
    }
  };

  activeEntries.forEach(({ m, idx }) => {
    appendDateDividerIfNeeded(m.createdAt);
    const row = buildMessageRowDom(m, idx, 0);
    if (row) thread.appendChild(row);
  });

  // 2. 답변 생성 중 말풍선 (liveRow) — 반드시 대기열(queued) 메시지보다 위에 렌더링!
  if (liveState.isRunning) {
    appendDateDividerIfNeeded(liveState.startedAt || Date.now());
    const prevDetails = document.getElementById('live-steps-details');
    const wasStepsOpen = prevDetails ? prevDetails.hasAttribute('open') : false;
    const openStepIndices = new Set();
    const prevStepEls = document.querySelectorAll('#live-steps-list > details.step');
    prevStepEls.forEach((el, idx) => {
      if (el.hasAttribute('open')) openStepIndices.add(idx);
    });
    const liveRow = document.createElement('div');
    liveRow.className = 'msg assistant live-generating';
    const stepsArr = filterToolSteps(liveState.steps);
    const qCount = queuedEntries.length;
    const queueBadgeHtml = `<span class="live-queue-badge" id="live-queue-badge" style="display:none"></span>`;
    const liveEffort = liveState.effort || (typeof serverStatus !== 'undefined' && serverStatus && serverStatus.effort ? serverStatus.effort : '');
    const liveModelName = liveState.model || (typeof serverStatus !== 'undefined' && serverStatus && serverStatus.model) || '';
    const liveTagText = liveModelName ? `${liveModelName}${liveEffort ? ` (${liveEffort})` : ''}` : '';
    const modelBadgeHtml = liveTagText ? `<span class="model-tag">${escapeHtml(liveTagText)}</span>` : '';
    const elapsedSec = liveState.startedAt ? Math.max(0, Math.floor((Date.now() - liveState.startedAt) / 1000)) : 0;
    const elapsedText = elapsedSec >= 1 ? `${elapsedSec}s` : '';
    const streamHtml = liveState.streamText ? formatMarkdown(liveState.streamText) : '';
    const liveAgentInfo = getAssistantDisplayInfo(room, null);
    liveRow.innerHTML = `
      <div class="assistant-head">
        <span class="assistant-avatar ${liveAgentInfo.avatarCls}">${liveAgentInfo.avatarIcon}</span>
        <span class="assistant-name" title="${escapeHtml(liveAgentInfo.fullName)}">${escapeHtml(liveAgentInfo.name)}</span>
        ${liveAgentInfo.roleBadgeHtml}
        ${modelBadgeHtml}
        ${queueBadgeHtml}
        ${buildMsgTimeHtml(liveState.startedAt)}
      </div>
      <details class="steps" id="live-steps-details" ${wasStepsOpen ? 'open' : ''} ${stepsArr.length > 0 ? '' : 'style="display:none"'}>
        <summary><span class="steps-icon">${ICONS.tool}</span>${ICONS.chevRight}<span id="live-steps-count">${formatLiveStepsSummaryLabel(stepsArr)}</span></summary>
        <div class="steps-list" id="live-steps-list">${stepsArr.map((s, sIdx) => `<details class="step" ${openStepIndices.has(sIdx) ? 'open' : ''}>${buildStepItemHtml(s, false)}</details>`).join('')}</div>
      </details>
      <div class="md live-stream-md" id="live-stream-md" ${streamHtml ? '' : 'style="display:none"'}>${streamHtml}</div>
      <div class="thinking-line">
        <span class="spinner"></span>
        <span class="feed shimmer" id="live-feed-text">${escapeHtml(liveState.feed || '생각하는 중…')}</span>
        <span class="live-elapsed" id="live-elapsed-timer">${elapsedSec >= 1800 ? `${elapsedText} ⚠️` : elapsedText}</span>
      </div>
      ${elapsedSec >= 1800 ? `
        <div class="timeout-warning-banner" style="margin-top:8px;padding:8px 12px;border-radius:8px;background:rgba(239,68,68,0.15);border:1px solid rgba(239,68,68,0.3);color:#f87171;font-size:13px;display:flex;align-items:center;justify-content:space-between;gap:8px;">
          <span>⚠️ 30분 이상 경과되었습니다. 프로세스가 중단되었거나 비정상 종료되었을 수 있습니다.</span>
          <button type="button" class="btn-abort-inline" style="padding:4px 8px;border-radius:6px;background:#ef4444;color:#fff;border:none;cursor:pointer;font-size:12px;" onclick="handleAbortButtonClick(event)">중단하기</button>
        </div>
      ` : ''}
    `;
    if (room && ORCHESTRATOR_ROOM_IDS.includes(room.id)) {
      const liveDelegated = extractDelegatedRoomsFromSteps(liveState.steps);
      if (liveDelegated && liveDelegated.length > 0) {
        const liveChipsDom = buildDelegatedSubAgentChipsDom(liveDelegated, room.id);
        if (liveChipsDom) liveRow.appendChild(liveChipsDom);
      }
    }
    thread.appendChild(liveRow);
  }

  // 3. 대기열(Queued)에 등록된 새 질문들은 답변 생성 중 말풍선 아래쪽에 렌더링!
  if (queuedEntries.length > 0) {
    const divider = document.createElement('div');
    divider.className = 'queue-divider';
    thread.appendChild(divider);

    queuedEntries.forEach(({ m, idx }, qIdx) => {
      const row = buildMessageRowDom(m, idx, qIdx + 1);
      if (row) thread.appendChild(row);
    });
  }

  container.innerHTML = '';
  container.appendChild(thread);
  scrollToBottomIfNear(forceScroll);
}

function cancelQueuedMessageByIndex(idx) {
  const room = getCurrentRoom();
  if (!room || !Array.isArray(room.messages)) return;
  const targetMsg = room.messages[idx];
  if (!targetMsg || !targetMsg.queued) return;

  let queueIndex = 0;
  for (let i = 0; i < idx; i++) {
    if (room.messages[i] && room.messages[i].queued) queueIndex++;
  }

  const removedText = targetMsg.text || '';
  room.messages.splice(idx, 1);

  const liveState = ensureRoomLiveState(room.id);
  liveState.queueCount = renumberQueuedMessages(room);

  saveChatRoomsToStorage();
  renderMessagesForCurrentRoom();
  renderRoomList();
  updateComposerStatusBar();
  updateSendButtonState();

  if (socket && socket.readyState === WebSocket.OPEN) {
    try {
      socket.send(JSON.stringify({
        type: 'cancel_queued',
        roomId: room.id,
        text: removedText,
        queueIndex: queueIndex
      }));
    } catch (_) {}
  }
  showToast('대기열에서 질문을 취소했습니다.');
}

function cancelAllQueuedMessages(roomId = currentRoomId) {
  const room = getRoomById(roomId) || getCurrentRoom();
  if (!room || !Array.isArray(room.messages)) return;
  const hadQueued = getQueuedMessagesCount(room);
  if (hadQueued === 0) return;

  room.messages = room.messages.filter(m => !(m && m.queued));
  const liveState = ensureRoomLiveState(room.id);
  liveState.queueCount = 0;

  saveChatRoomsToStorage();
  renderMessagesForCurrentRoom();
  renderRoomList();
  updateComposerStatusBar();
  updateSendButtonState();

  if (socket && socket.readyState === WebSocket.OPEN) {
    try {
      socket.send(JSON.stringify({
        type: 'cancel_queued',
        roomId: room.id,
        all: true
      }));
    } catch (_) {}
  }
  showToast(`대기열 질문 ${hadQueued}건을 취소했습니다.`);
}

function onSuggestionClick(idx) {
  const items = currentRoomId === CORE_ROOM_ID
    ? CORE_ORCHESTRATOR_SUGGESTIONS
    : DEFAULT_SUGGESTIONS;
  const item = items[idx];
  if (item && item.prompt) sendUserMessage(item.prompt, false);
}

function copyMessageByIndex(idx) {
  const room = getCurrentRoom();
  const m = room && room.messages && room.messages[idx];
  if (m && m.text) copyToClipboard(m.text, '메시지가');
}

function speakMessageByIndex(idx) {
  const room = getCurrentRoom();
  const m = room && room.messages && room.messages[idx];
  if (m && m.text) playTts(m.text);
}

function editMessageToInputByIndex(idx) {
  const room = getCurrentRoom();
  const m = room && room.messages && room.messages[idx];
  if (!m || !m.text) return;
  const input = document.getElementById('chat-input');
  if (input) {
    setChatInputText(m.text);
    onChatInput();
    try { input.focus({ preventScroll: true }); } catch (_) { input.focus(); }
    showToast('입력창으로 내용을 가져왔습니다.');
  }
}

function retryMessageByIndex(idx) {
  const room = getCurrentRoom();
  if (!room || !Array.isArray(room.messages)) return;
  let targetPrompt = '';
  const m = room.messages[idx];
  if (!m) return;
  if ((m.sender || m.role) === 'user') {
    targetPrompt = m.text || '';
  } else {
    for (let i = idx - 1; i >= 0; i--) {
      const prev = room.messages[i];
      if (prev && (prev.sender || prev.role) === 'user' && prev.text) {
        targetPrompt = prev.text;
        break;
      }
    }
  }
  if (targetPrompt) sendUserMessage(targetPrompt, false, { skipFocus: true });
}

// ─── 모바일 메시지 길게 누르기 액션 바텀시트 ───
function openMsgActionSheet(idx) {
  const room = getCurrentRoom();
  const m = room && room.messages && room.messages[idx];
  if (!m || !m.text) return;
  activeSheetMsgIndex = idx;
  triggerHaptic(22);
  const previewEl = document.getElementById('sheet-msg-preview');
  if (previewEl) previewEl.innerText = m.text.replace(/\s+/g, ' ').trim().slice(0, 60);
  document.getElementById('sheet-msg-actions')?.classList.add('open');
}

function closeMsgActionSheet() {
  document.getElementById('sheet-msg-actions')?.classList.remove('open');
  activeSheetMsgIndex = -1;
}

function onMsgSheetBackdropClick(e) {
  if (e.target && e.target.id === 'sheet-msg-actions') closeMsgActionSheet();
}

function onSheetActionCopy() {
  const idx = activeSheetMsgIndex;
  closeMsgActionSheet();
  if (idx >= 0) copyMessageByIndex(idx);
}

function onSheetActionSpeak() {
  const idx = activeSheetMsgIndex;
  closeMsgActionSheet();
  if (idx >= 0) speakMessageByIndex(idx);
}

function onSheetActionEditToInput() {
  const idx = activeSheetMsgIndex;
  closeMsgActionSheet();
  if (idx >= 0) editMessageToInputByIndex(idx);
}

function onSheetActionRetry() {
  const idx = activeSheetMsgIndex;
  closeMsgActionSheet();
  if (idx >= 0) retryMessageByIndex(idx);
}

function bindMessageLongPressEvents() {
  const container = document.getElementById('messages-container');
  if (!container || container._msgLongPressBound) return;
  container._msgLongPressBound = true;

  let timer = null;
  let startX = 0, startY = 0;

  const clear = () => { if (timer) { clearTimeout(timer); timer = null; } };

  container.addEventListener('touchstart', (e) => {
    if (!e.touches || e.touches.length !== 1) return;
    if (e.target.closest('button') || e.target.closest('a') || e.target.closest('summary') || e.target.closest('pre')) return;
    const msgEl = e.target.closest('.msg[data-msg-idx]');
    if (!msgEl) return;
    const idx = parseInt(msgEl.getAttribute('data-msg-idx'), 10);
    if (isNaN(idx)) return;
    startX = e.touches[0].clientX;
    startY = e.touches[0].clientY;
    clear();
    timer = setTimeout(() => {
      const sel = window.getSelection ? String(window.getSelection()) : '';
      if (!sel) openMsgActionSheet(idx);
    }, 500);
  }, { passive: true });

  container.addEventListener('touchmove', (e) => {
    if (!timer || !e.touches || !e.touches[0]) return;
    if (Math.abs(e.touches[0].clientX - startX) > 10 || Math.abs(e.touches[0].clientY - startY) > 10) clear();
  }, { passive: true });

  container.addEventListener('touchend', clear, { passive: true });
  container.addEventListener('touchcancel', clear, { passive: true });
}
