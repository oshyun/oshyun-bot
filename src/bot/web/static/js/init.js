// ─── 전역 단축키 ───
window.addEventListener('keydown', (e) => {
  const confirmModal = document.getElementById('modal-confirm');
  const isConfirmModalOpen = confirmModal && confirmModal.style.display === 'flex';

  if (e.key === 'Enter' && isConfirmModalOpen) {
    e.preventDefault();
    e.stopPropagation();
    const okBtn = document.getElementById('modal-confirm-ok');
    if (okBtn) okBtn.click();
    return;
  }

  if (e.key === 'Escape') {
    if (isVoiceRecording) {
      currentSpeechTranscript = '';
      hasDispatchedSpeech = true;
      stopVoiceRecording(true);
      setChatInputText(''); onChatInput();
      return;
    }
    closeConfirmModal(false);
    closeRenameRoomModal();
    closeModal('modal-spawn-session');
    closeModal('modal-task-note');
    closeModal('modal-agent-config');
    closeImageLightbox();
    if (typeof closeThreadPanel === 'function') {
      closeThreadPanel();
    }
    if (typeof closeNoteViewer === 'function') {
      closeNoteViewer();
    }
    if (isRoomSelectionMode) {
      exitRoomSelectionMode();
      return;
    }
    closeRoomDrawer();
  }
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 's' && currentView === 'vault' && currentEditingPath) {
    e.preventDefault();
    saveCurrentVaultFile();
  }
  if ((e.metaKey || e.ctrlKey) && e.shiftKey && e.key.toLowerCase() === 'o') {
    e.preventDefault();
    createNewChat();
  }
});

function setupDragAndDropAndPaste() {
  const chatSection = document.getElementById('view-chat');
  if (chatSection) {
    ['dragenter', 'dragover'].forEach(eventName => {
      chatSection.addEventListener(eventName, (e) => {
        e.preventDefault();
        e.stopPropagation();
        chatSection.classList.add('drag-over');
      }, false);
    });

    ['dragleave', 'drop'].forEach(eventName => {
      chatSection.addEventListener(eventName, (e) => {
        e.preventDefault();
        e.stopPropagation();
        chatSection.classList.remove('drag-over');
      }, false);
    });

    chatSection.addEventListener('drop', (e) => {
      const dt = e.dataTransfer;
      if (dt && dt.files && dt.files.length > 0) {
        addAttachmentFiles(dt.files);
      }
    }, false);
  }

  function extractImagesFromDataTransfer(dt) {
    const imgFiles = [];
    if (!dt) return imgFiles;
    const items = dt.items;
    if (items) {
      for (let i = 0; i < items.length; i++) {
        const item = items[i];
        if (item.kind === 'file' || (item.type && item.type.startsWith('image/'))) {
          const file = item.getAsFile();
          if (file && (!file.type || file.type.startsWith('image/'))) {
            imgFiles.push(file);
          }
        }
      }
    }
    if (imgFiles.length === 0 && dt.files && dt.files.length > 0) {
      for (let i = 0; i < dt.files.length; i++) {
        const f = dt.files[i];
        if (f && ((f.type && f.type.startsWith('image/')) || /\.(png|jpe?g|gif|webp|heic|heif|bmp|svg)$/i.test(f.name || ''))) {
          imgFiles.push(f);
        }
      }
    }
    return imgFiles;
  }

  function insertPlainTextAtCursor(text) {
    if (!text) return;
    const sel = window.getSelection();
    if (sel && sel.rangeCount > 0) {
      const range = sel.getRangeAt(0);
      range.deleteContents();
      const textNode = document.createTextNode(text);
      range.insertNode(textNode);
      range.setStartAfter(textNode);
      range.collapse(true);
      sel.removeAllRanges();
      sel.addRange(range);
    } else {
      document.execCommand('insertText', false, text);
    }
  }

  window.addEventListener('paste', (e) => {
    const dt = e.clipboardData || window.clipboardData;
    const imgFiles = extractImagesFromDataTransfer(dt);
    if (imgFiles.length > 0) {
      e.preventDefault();
      addAttachmentFiles(imgFiles);
      showToast(`${imgFiles.length}개의 클립보드 이미지가 첨부되었습니다.`);
      return;
    }
    // contenteditable 입력창에 HTML 서식이 섞여 들어가지 않도록 순수 텍스트만 삽입
    const chatInput = document.getElementById('chat-input');
    if (chatInput && chatInput.tagName !== 'TEXTAREA' && (e.target === chatInput || chatInput.contains(e.target))) {
      const plain = dt ? dt.getData('text/plain') : '';
      if (plain) {
        e.preventDefault();
        const normalized = plain.replace(/\r\n/g, '\n');
        if (normalized.length >= LONG_PASTE_CHAR_THRESHOLD) {
          const lineCount = normalized.split('\n').length;
          pendingLongPastes.push({
            text: normalized,
            charCount: normalized.length,
            lineCount,
            preview: normalized.slice(0, 80).replace(/\s+/g, ' ').trim(),
          });
          renderComposerAttachments();
          updateSendButtonState();
          showToast(`긴 텍스트(${normalized.length.toLocaleString()}자)를 첨부 칩으로 빠르게 추가했습니다.`);
          return;
        }
        const curPlain = (chatInput.textContent || '').replace(/\u200B/g, '');
        if (!curPlain && !chatInput.querySelector('img')) {
          chatInput.textContent = normalized;
          try {
            const sel = window.getSelection();
            if (sel) {
              const range = document.createRange();
              range.selectNodeContents(chatInput);
              range.collapse(false);
              sel.removeAllRanges();
              sel.addRange(range);
            }
          } catch (_) {}
        } else {
          insertPlainTextAtCursor(normalized);
        }
        onChatInput();
      } else {
        // iOS Safari 등에서 clipboardData 없이 <img> 노드를 직접 삽입하는 경우 대비
        setTimeout(() => onChatInput(), 0);
      }
    }
  });
}

// ─── 초기화 ───
window.addEventListener('DOMContentLoaded', () => {
  // 1단계(즉시 실행): 입력창 활성화·포커스 및 WebSocket 연결을 최우선 시작
  updateSendButtonState();
  const chatInput = document.getElementById('chat-input');
  if (chatInput) {
    if (isTouchOrMobileDevice()) {
      chatInput.setAttribute('enterkeyhint', 'enter');
    }
    chatInput.addEventListener('compositionstart', () => { isImeComposing = true; });
    chatInput.addEventListener('compositionend', () => { isImeComposing = false; });
  }
  connectWs();
  focusChatInput({ isInitialLoad: true });

  // 2단계(첫 페인트 직후 백그라운드): 무거운 히스토리 렌더링, 서버 동기화, 제스처 바인딩 지연 실행
  const runDeferredInit = () => {
    setupDragAndDropAndPaste();
    loadChatRoomsFromStorage();
    try {
      const deepLinkRoom = new URLSearchParams(window.location.search).get('room');
      if (deepLinkRoom) {
        currentRoomId = deepLinkRoom;
      }
    } catch (_) {}
    loadChatRoomsFromServer(false);

    const fi = document.getElementById('file-input');
    if (fi) {
      fi.addEventListener('cancel', () => {
        isFilePickerActive = false;
      });
    }

    // 창 포커스 이탈(파일 선택기/사진첩 열기 등) 시에는 현재 대화방 컨텍스트를 완벽 보존하고, 복귀 시에만 다른 비활성 빈 방을 정리
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') {
        const preservedRoomId = currentRoomId;
        if (!isFilePickerActive) {
          cleanUnusedEmptyRooms(preservedRoomId);
        }
        loadChatRoomsFromServer(false).then(() => {
          if (preservedRoomId && currentRoomId !== preservedRoomId) {
            currentRoomId = preservedRoomId;
            renderRoomList();
            renderMessagesForCurrentRoom(true);
          }
          if (isFilePickerActive) {
            setTimeout(() => { isFilePickerActive = false; }, 300);
          }
        });
      }
    });

    window.addEventListener('pagehide', () => {
      cleanCurrentIfUnusedEmpty();
    });

    window.addEventListener('beforeunload', () => {
      cleanCurrentIfUnusedEmpty();
    });

    const renameInput = document.getElementById('input-rename-room');
    if (renameInput) {
      renameInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.isComposing) {
          e.preventDefault();
          submitRenameRoom();
        }
      });
    }

    bindRoomLongPressEvents();
    bindMessageLongPressEvents();
    initMobileSwipeGestures();
    initPullToRefresh();
    initPinchZoomFontSize();
    initVisualViewportHandler();
    if (typeof initMessagesScrollListeners === 'function') {
      initMessagesScrollListeners();
    }

    document.addEventListener('pointerdown', (e) => {
      const cfgMenu = document.getElementById('room-config-popover');
      if (cfgMenu && cfgMenu.classList.contains('open')) {
        const cfgWrap = document.getElementById('topbar-config-wrap');
        if (cfgWrap && !cfgWrap.contains(e.target)) {
          closeRoomConfigPopover();
        }
      }
      // 프롬프트 창(#chat-input) 바깥 영역 터치/클릭 시 포커스 해제하여 모바일/iPad 가상 키보드 닫기
      const chatInput = document.getElementById('chat-input');
      const activeEl = document.activeElement;
      if (chatInput && (activeEl === chatInput || chatInput.contains(activeEl))) {
        const target = e.target;
        if (
          target &&
          !target.closest('.composer') &&
          !target.closest('input, textarea, select, [contenteditable="true"]')
        ) {
          try { chatInput.blur(); } catch (_) {}
        }
      }
    });

    updateSessionBar();
    updateTtsUi();
    updateMobileAutofocusUi();
    updateUiFontSizeUi();
    applyUiFontSize(getUiFontSize());
    updateFontSizeUi();
    applyFontSize(getFontSize());
    updateSttTimeoutUi();
    loadStatusAndSettings();
    if (typeof loadAgentConfigsFromServer === 'function') {
      loadAgentConfigsFromServer();
    }
    initUsagePolling();
    updatePwaUi();
    updateNotificationUi();

    try {
      const urlParams = new URLSearchParams(window.location.search);
      const initialRoom = urlParams.get('room');
      if (initialRoom) {
        if (typeof syncRoomHistory === 'function') {
          syncRoomHistory(initialRoom, 'replace');
        }
        if (typeof updatePwaManifestForRoom === 'function') {
          updatePwaManifestForRoom(initialRoom);
        }
        setTimeout(() => {
          if (typeof selectRoom === 'function') {
            selectRoom(initialRoom, { historyMode: 'replace' });
          }
        }, 100);
      } else if (currentRoomId && typeof syncRoomHistory === 'function') {
        syncRoomHistory(currentRoomId, 'replace');
        if (typeof updatePwaManifestForRoom === 'function') {
          updatePwaManifestForRoom(currentRoomId);
        }
      }
      const isVoicePath = window.location.pathname === '/voice' || window.location.pathname === '/voice/';
      const isVoiceParam = urlParams.has('voice') && urlParams.get('voice') !== '0' && urlParams.get('voice') !== 'false';
      const isAutoParam = urlParams.get('auto') === '1' || urlParams.get('auto') === 'true';
      if (isVoicePath || isVoiceParam || isAutoParam) {
        const cleanUrl = new URL(window.location.href);
        if (isVoicePath) cleanUrl.pathname = '/';
        cleanUrl.searchParams.delete('voice');
        cleanUrl.searchParams.delete('auto');
        window.history.replaceState(window.history.state || {}, '', cleanUrl.toString());
        setTimeout(() => {
          focusChatInput();
          tryStartVoice(false);
        }, 300);
      }
    } catch (_) {}
  };

  if (typeof requestAnimationFrame === 'function') {
    requestAnimationFrame(() => setTimeout(runDeferredInit, 0));
  } else {
    setTimeout(runDeferredInit, 0);
  }
});

window.addEventListener('popstate', (event) => {
  try {
    const stateRoomId = event && event.state && event.state.roomId;
    const urlRoomId = new URLSearchParams(window.location.search).get('room');
    const targetRoomId = stateRoomId || urlRoomId;
    if (targetRoomId && typeof selectRoom === 'function') {
      selectRoom(targetRoomId, { historyMode: 'none' });
    }
  } catch (_) {}
});

window.addEventListener('load', () => {
  if (currentView === 'chat') {
    scrollToBottom(false);
  }
});
