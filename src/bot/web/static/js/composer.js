// ─── 대화 입력 & 전송 ───
let isImeComposing = false;
let lastSentMessageTime = 0;
let lastSentMessageText = '';
let pendingAttachments = []; // [{ file, name, size, type, previewUrl }]
let pendingLongPastes = []; // [{ text, charCount, lineCount, preview }]
const LONG_PASTE_CHAR_THRESHOLD = 2500;

let isFilePickerActive = false;
function triggerFileInput() {
  const fi = document.getElementById('file-input');
  if (fi) {
    isFilePickerActive = true;
    fi.value = '';
    fi.click();
  }
}

function handleFileSelect(e) {
  isFilePickerActive = false;
  const files = e.target.files;
  if (files && files.length > 0) {
    addAttachmentFiles(files);
  }
}

function addAttachmentFiles(files) {
  for (let i = 0; i < files.length; i++) {
    const file = files[i];
    const previewUrl = file.type && file.type.startsWith('image/') ? URL.createObjectURL(file) : '';
    pendingAttachments.push({
      file: file,
      name: file.name,
      size: file.size,
      type: file.type || 'application/octet-stream',
      previewUrl: previewUrl,
    });
  }
  renderComposerAttachments();
  updateSendButtonState();
  focusChatInput();
}

function removeAttachment(idx) {
  if (idx >= 0 && idx < pendingAttachments.length) {
    const removed = pendingAttachments.splice(idx, 1)[0];
    if (removed && removed.previewUrl) {
      try { URL.revokeObjectURL(removed.previewUrl); } catch (_) {}
    }
    renderComposerAttachments();
    updateSendButtonState();
  }
}

function removeLongPaste(idx) {
  if (idx >= 0 && idx < pendingLongPastes.length) {
    pendingLongPastes.splice(idx, 1);
    renderComposerAttachments();
    updateSendButtonState();
  }
}

function clearComposerAttachments() {
  pendingAttachments.forEach(att => {
    if (att.previewUrl) {
      try { URL.revokeObjectURL(att.previewUrl); } catch (_) {}
    }
  });
  pendingAttachments = [];
  pendingLongPastes = [];
  renderComposerAttachments();
  updateSendButtonState();
}

function renderComposerAttachments() {
  const wrap = document.getElementById('composer-attachments');
  if (!wrap) return;
  if (!pendingAttachments.length && !pendingLongPastes.length) {
    wrap.innerHTML = '';
    return;
  }
  const longPasteHtml = pendingLongPastes.map((item, idx) => `
    <div class="attach-preview-item" title="${escapeHtml(item.preview || '붙여넣은 긴 텍스트')}">
      <div style="width:38px;height:38px;display:flex;align-items:center;justify-content:center;background:var(--bg-hover);border-radius:8px;font-size:17px;flex-shrink:0;">📋</div>
      <div class="attach-file-info">
        <span class="attach-file-name">긴 텍스트 (${item.charCount.toLocaleString()}자)</span>
        <span class="attach-file-size">${item.lineCount.toLocaleString()}줄 · 전송 시 자동 포함</span>
      </div>
      <button type="button" class="attach-del-btn" onclick="removeLongPaste(${idx})" title="삭제">✕</button>
    </div>
  `).join('');
  const attHtml = pendingAttachments.map((att, idx) => {
    const sizeKb = Math.round(att.size / 1024);
    const sizeStr = sizeKb >= 1024 ? (sizeKb / 1024).toFixed(1) + 'MB' : sizeKb + 'KB';
    const isImg = Boolean(att.previewUrl);
    const iconOrThumb = isImg
      ? `<img src="${att.previewUrl}" alt="${escapeHtml(att.name)}">`
      : `<div style="width:44px;height:44px;display:flex;align-items:center;justify-content:center;background:var(--bg-hover);border-radius:8px;font-size:18px;">📄</div>`;
    return `
      <div class="attach-preview-item" title="${escapeHtml(att.name)}">
        ${iconOrThumb}
        <div class="attach-file-info">
          <span class="attach-file-name">${escapeHtml(att.name)}</span>
          <span class="attach-file-size">${sizeStr}</span>
        </div>
        <button type="button" class="attach-del-btn" onclick="removeAttachment(${idx})" title="삭제">✕</button>
      </div>
    `;
  }).join('');
  wrap.innerHTML = longPasteHtml + attHtml;
}

function openImageLightbox(url) {
  const modal = document.getElementById('image-lightbox-modal');
  const img = document.getElementById('image-lightbox-img');
  if (modal && img && url) {
    img.src = url;
    modal.classList.add('active');
  }
}

function closeImageLightbox() {
  const modal = document.getElementById('image-lightbox-modal');
  const img = document.getElementById('image-lightbox-img');
  if (modal) modal.classList.remove('active');
  if (img) img.src = '';
}

async function uploadPendingAttachments() {
  if (!pendingAttachments.length) return [];
  const formData = new FormData();
  for (const att of pendingAttachments) {
    formData.append('file', att.file, att.name);
  }
  const resp = await fetch('/api/upload', {
    method: 'POST',
    body: formData,
  });
  if (!resp.ok) {
    const errText = await resp.text();
    throw new Error(`파일 업로드 실패 (HTTP ${resp.status}): ${errText}`);
  }
  const data = await resp.json();
  if (!data.ok || !data.files) {
    throw new Error(data.error || '파일 업로드 처리 오류');
  }
  return data.files;
}

function getRawChatInputTypedText(input) {
  if (!input) return '';
  if ('value' in input && input.tagName === 'TEXTAREA') return input.value || '';
  // 자식 HTML 노드가 없으면 textContent로 읽어 동기 레이아웃/리플로우(innerText)를 원천 차단
  const rawSource = input.childElementCount === 0 ? (input.textContent || '') : (input.innerText || '');
  const raw = rawSource.replace(/\u200B/g, '').replace(/\r\n/g, '\n');
  if (!raw.trim() && !input.querySelector('img')) {
    if ((input.textContent || '').replace(/\u200B/g, '') === '') return '';
  }
  return raw.replace(/\n$/, '');
}

function getChatInputText() {
  const input = document.getElementById('chat-input');
  const typed = getRawChatInputTypedText(input);
  if (!pendingLongPastes || pendingLongPastes.length === 0) return typed;
  const longPart = pendingLongPastes.map(p => p.text).join('\n\n');
  const trimmedTyped = typed.trim();
  return trimmedTyped ? `${longPart}\n\n${typed}` : longPart;
}

function setChatInputText(text) {
  const input = document.getElementById('chat-input');
  if (!input) return;
  if ('value' in input && input.tagName === 'TEXTAREA') {
    input.value = text || '';
    return;
  }
  if (!text) {
    input.innerHTML = '';
  } else {
    input.textContent = text;
  }
}

function setChatInputPlaceholder(text) {
  const input = document.getElementById('chat-input');
  if (!input) return;
  if (input.tagName === 'TEXTAREA') {
    input.placeholder = text || '';
  } else {
    input.setAttribute('data-placeholder', text || '');
  }
}

function dataUrlToFile(dataUrl, filenamePrefix = 'pasted') {
  try {
    const parts = dataUrl.split(',');
    if (parts.length < 2) return null;
    const mimeMatch = parts[0].match(/:(.*?);/);
    const mime = mimeMatch ? mimeMatch[1] : 'image/png';
    const bstr = atob(parts[1]);
    let n = bstr.length;
    const u8arr = new Uint8Array(n);
    while (n--) u8arr[n] = bstr.charCodeAt(n);
    const ext = (mime.split('/')[1] || 'png').replace('jpeg', 'jpg').split('+')[0];
    const filename = `${filenamePrefix}-${Date.now()}.${ext}`;
    return new File([u8arr], filename, { type: mime });
  } catch (_) {
    return null;
  }
}

async function extractEmbeddedChatImages(input) {
  if (!input || input.tagName === 'TEXTAREA') return;
  const imgs = Array.from(input.querySelectorAll('img'));
  if (imgs.length === 0) return;
  const files = [];
  for (const img of imgs) {
    const src = img.getAttribute('src') || '';
    img.remove();
    if (src.startsWith('data:image/')) {
      const f = dataUrlToFile(src, 'clipboard');
      if (f) files.push(f);
    } else if (src.startsWith('blob:')) {
      try {
        const resp = await fetch(src);
        const blob = await resp.blob();
        if (blob && blob.type && blob.type.startsWith('image/')) {
          const ext = (blob.type.split('/')[1] || 'png').replace('jpeg', 'jpg');
          files.push(new File([blob], `clipboard-${Date.now()}.${ext}`, { type: blob.type }));
        }
      } catch (_) {}
    }
  }
  if (files.length > 0) {
    addAttachmentFiles(files);
    showToast(`${files.length}개의 클립보드 이미지가 첨부되었습니다.`);
  }
}

function onChatInput() {
  const input = document.getElementById('chat-input');
  if (!input) return;
  if (input.tagName !== 'TEXTAREA') {
    if (input.querySelector('img')) {
      extractEmbeddedChatImages(input);
    }
    // contenteditable에서 텍스트를 모두 지운 뒤 남는 <br> 정리 (placeholder 복원)
    const txt = (input.textContent || '').replace(/\u200B/g, '');
    if (!txt && !input.querySelector('img') && input.innerHTML !== '') {
      input.innerHTML = '';
    }
  } else {
    input.style.height = '36px';
    if (input.value && input.value.trim()) {
      input.style.height = Math.min(Math.max(input.scrollHeight, 36), 200) + 'px';
    }
  }
  updateSendButtonState();
}

function updateSendButtonState() {
  const btn = document.getElementById('btn-send');
  const input = document.getElementById('chat-input');
  if (!btn || !input) return;
  const live = roomLiveStates[currentRoomId];
  const isRunning = Boolean(live && live.isRunning);
  const typedRaw = (input.tagName === 'TEXTAREA' ? (input.value || '') : (input.textContent || '')).replace(/\u200B/g, '');
  const hasLongPastes = Boolean(pendingLongPastes && pendingLongPastes.length > 0);
  const hasText = Boolean(typedRaw.trim()) || hasLongPastes;
  const hasAttachments = Boolean(pendingAttachments && pendingAttachments.length > 0);
  const canSend = hasText || hasAttachments;

  if (isRunning && !canSend) {
    btn.disabled = false;
    btn.classList.add('stop');
    btn.classList.remove('queue');
    btn.title = '생성 중단';
    if (btn.dataset.iconState !== 'stop') {
      btn.dataset.iconState = 'stop';
      btn.innerHTML = ICONS.stop;
    }
  } else {
    btn.disabled = !canSend;
    btn.classList.remove('stop');
    btn.classList.toggle('queue', isRunning && canSend);
    btn.title = isRunning ? '대기열에 추가' : '전송';
    if (btn.dataset.iconState !== 'arrowUp') {
      btn.dataset.iconState = 'arrowUp';
      btn.innerHTML = ICONS.arrowUp;
    }
  }
}

function onSendButtonClick() {
  const live = roomLiveStates[currentRoomId];
  const hasAttachments = Boolean(pendingAttachments && pendingAttachments.length > 0);
  const hasText = Boolean(getChatInputText().trim()) || hasAttachments;
  if (live && live.isRunning && !hasText) {
    abortCurrentSession(currentRoomId);
  } else {
    sendUserMessage();
  }
}

let _nextSendOptions = null;

async function sendUserMessage(textOverride, isVoiceTurn = false, options = {}) {
  const skipFocus = Boolean(isVoiceTurn || (options && options.skipFocus));
  maybeRequestNotificationPermission();
  const text = (textOverride !== undefined ? textOverride : getChatInputText()).trim();
  const hasAttachments = Boolean(pendingAttachments && pendingAttachments.length > 0);
  if (!text && !hasAttachments) return;

  if (isVoiceRecording || recognition) {
    hasDispatchedSpeech = true;
    currentSpeechTranscript = '';
    stopVoiceRecording(true);
  }

  const now = Date.now();
  if (!hasAttachments && (now - lastSentMessageTime < 1500) && text === lastSentMessageText) {
    setChatInputText(''); isImeComposing = false; onChatInput();
    return;
  }
  if (!textOverride && !hasAttachments && (now - lastSentMessageTime < 500)) {
    if (lastSentMessageText && lastSentMessageText.endsWith(text) && text.length <= 4) {
      setChatInputText(''); isImeComposing = false; onChatInput();
      return;
    }
  }

  if (!socket || socket.readyState !== WebSocket.OPEN) {
    if (!hasAttachments && text) {
      pendingChatQueue.push({ text, isVoiceTurn });
      setChatInputText('');
      isImeComposing = false;
      onChatInput();
      showToast('서버 연결 중… 연결 즉시 자동 전송됩니다.');
      connectWs();
      return;
    }
    showToast('서버에 연결되지 않았습니다. 재연결을 시도합니다…');
    connectWs();
    return;
  }

  let uploadedFiles = [];
  if (hasAttachments) {
    try {
      showToast('사진/파일 업로드 중…');
      uploadedFiles = await uploadPendingAttachments();
    } catch (uploadErr) {
      console.error('[Upload Error]', uploadErr);
      showToast('첨부파일 업로드 실패: ' + (uploadErr.message || String(uploadErr)));
      return;
    }
  }

  lastSentMessageTime = now;
  lastSentMessageText = text;
  currentSpeechTranscript = '';

  const input = document.getElementById('chat-input');
  setChatInputText('');
  isImeComposing = false;
  onChatInput();
  if (skipFocus && input && document.activeElement === input) {
    input.blur();
  }
  clearComposerAttachments();

  const clientMsgId = 'c-' + now + '-' + Math.random().toString(36).slice(2, 7);
  const room = getCurrentRoom();
  if (room) {
    const liveState = ensureRoomLiveState(room.id);
    const existingQueuedCount = getQueuedMessagesCount(room);
    const isQueuedSend = Boolean(liveState.isRunning || existingQueuedCount > 0);
    const userMsg = {
      clientMsgId: clientMsgId,
      sender: 'user',
      role: 'user',
      text: text,
      attachments: uploadedFiles,
      createdAt: now
    };
    if (isQueuedSend) {
      userMsg.queued = true;
      userMsg.queuePos = existingQueuedCount + 1;
      userMsg._localPendingAt = now;
      liveState.queueCount = existingQueuedCount + 1;
    } else {
      liveState.isRunning = true;
      liveState.jobId = '';
      liveState.asstMsgId = null;
      liveState.feed = '생각하는 중…';
      liveState.steps = [];
      liveState.streamText = '';
      liveState.startedAt = now;
      liveState.queueCount = 0;
      syncLiveElapsedTimer();
    }
    upsertRoomMessage(room, userMsg);
    room.messageCount = room.messages.length;
    room.updatedAt = now;
    delete room.userCreatedEmpty;
    if (room.title === '새 대화' || !room.title) {
      const titleBase = text || (uploadedFiles.length ? `${uploadedFiles[0].name} 분석` : '사진 분석');
      room.title = titleBase.slice(0, 22) + (titleBase.length > 22 ? '…' : '');
    }
    sortChatRooms();
  }

  saveChatRoomsToStorage();
  try {
    renderRoomList();
    updateTopbarTitle();
    renderMessagesForCurrentRoom(true);
    updateComposerStatusBar();
    updateSendButtonState();
  } catch (uiErr) {
    console.error('[sendUserMessage UI]', uiErr);
  }

  const targetRoomId = room ? room.id : 'room-default';
  const targetSessionId = room ? (room.sessionId || null) : null;
  const isNew = Boolean(!room || room.isNewSession || !targetSessionId);
  const targetGroupId = isNew ? null : (room ? (room.activeGroupId || null) : null);
  if (isNew && room) {
    room.activeGroupId = null;
  }
  const isVoice = Boolean(isVoiceTurn);
  lastRequestedRoomId = targetRoomId;

  const opts = (_nextSendOptions && typeof _nextSendOptions === 'object') ? _nextSendOptions : {};
  _nextSendOptions = null;
  const attachedPaths = uploadedFiles.map(f => f.path).filter(Boolean);
  try {
    socket.send(JSON.stringify({
      type: 'chat',
      text: text,
      prompt: text,
      clientMsgId: clientMsgId,
      voiceMode: isVoice,
      roomId: targetRoomId,
      sessionId: targetSessionId,
      groupId: targetGroupId,
      newSession: isNew,
      attachments: uploadedFiles,
      attachedFiles: attachedPaths,
      ttsVoice: (document.getElementById('select-tts-voice') ? document.getElementById('select-tts-voice').value : '') || 'ko-KR-SunHiNeural',
      model: opts.model || ((room && room.model) ? room.model : undefined),
      effort: opts.effort || ((room && room.effort) ? room.effort : undefined),
      rawPrompt: Boolean(opts.rawPrompt),
      slashCommands: Boolean(opts.slashCommands),
      orchestrator: (opts.orchestrator !== undefined) ? Boolean(opts.orchestrator) : undefined
    }));
  } catch (err) {
    showToast('메시지 전송 실패: ' + (err.message || String(err)));
  }
  if (!skipFocus) {
    focusChatInput();
  }
}

function handleInputKeyDown(e) {
  if (e.key === 'Enter') {
    if (e.isComposing || e.keyCode === 229 || isImeComposing) return;
    // 모바일/터치 기기에서는 Enter가 줄바꿈으로 동작 (전송은 전송 버튼 또는 Ctrl/Cmd+Enter)
    if (isTouchOrMobileDevice()) {
      if (e.ctrlKey || e.metaKey) {
        e.preventDefault();
        sendUserMessage();
      }
      return;
    }
    // 데스크탑: Enter 전송, Shift+Enter 줄바꿈
    if (!e.shiftKey) {
      e.preventDefault();
      sendUserMessage();
    }
  }
}
