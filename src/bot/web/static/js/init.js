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
    if (typeof closeRoomConfigPopover === 'function') {
      closeRoomConfigPopover();
    }
    if (typeof closeTopbarCustomizer === 'function') {
      closeTopbarCustomizer();
    }
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

// ─── 전역 초기 로딩 안전망 (Watchdog & Skeletons Removal) ───
let isAppInitialized = false;

function hideInitialSkeletons() {
  const skeletonEl = document.getElementById('chat-loading-skeleton');
  if (skeletonEl) skeletonEl.remove();
  const sbSkeleton = document.getElementById('sb-initial-skeleton');
  if (sbSkeleton) sbSkeleton.remove();
}

function markAppReady() {
  if (isAppInitialized) return;
  isAppInitialized = true;
  document.body.classList.add('app-ready');
  hideInitialSkeletons();
}
window.markAppReady = markAppReady;
window.hideInitialSkeletons = hideInitialSkeletons;

// ─── 초기화 ───
window.addEventListener('DOMContentLoaded', () => {
  // 3초 전역 세이프가드 워치독: 네트워크, 소켓, API 지연/실패 시에도 무조건 스켈레톤 해제 및 정상 빈 방/캐시 렌더링
  const initWatchdog = setTimeout(() => {
    if (!isAppInitialized) {
      console.warn('[Watchdog] 초기 로딩 타임아웃(2.8s) 도달 — 스켈레톤 강제 해제 및 안전망 렌더링');
      if (typeof isInitialRoomsLoading !== 'undefined') isInitialRoomsLoading = false;
      if (typeof ensureCoreRoom === 'function') ensureCoreRoom();
      if (Array.isArray(chatRooms) && chatRooms.length === 0) {
        const fallbackId = 'room-' + Date.now();
        chatRooms.push({
          id: fallbackId,
          roomId: fallbackId,
          title: '새 대화',
          preview: '',
          messageCount: 0,
          messages: [],
          isNewSession: true,
          userCreatedEmpty: true,
          sessionId: null,
          updatedAt: Date.now()
        });
        currentRoomId = fallbackId;
      }
      const cur = typeof getCurrentRoom === 'function' ? getCurrentRoom() : null;
      if (cur) cur.historyLoaded = true;
      if (typeof renderRoomList === 'function') renderRoomList();
      if (typeof renderMessagesForCurrentRoom === 'function') renderMessagesForCurrentRoom(true);
      markAppReady();
    }
  }, 2800);

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

    function checkUrlRoomAndNavigate() {
      try {
        const urlParams = new URLSearchParams(window.location.search);
        const urlRoom = urlParams.get('room');
        if (urlRoom && urlRoom !== currentRoomId && typeof selectRoom === 'function') {
          selectRoom(urlRoom, { historyMode: 'replace' });
          return true;
        }
      } catch (_) {}
      return false;
    }

    // 창 포커스 복귀(홈 화면 바로가기 재진입/앱 복원 등) 시 URL 딥링크 최우선 반영
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') {
        if (!isAppInitialized) {
          console.warn('[VisibilityChange] 탭 활성화 시점 초기화 미완료 — 안전망 즉각 가동');
          if (typeof isInitialRoomsLoading !== 'undefined') isInitialRoomsLoading = false;
          if (typeof ensureCoreRoom === 'function') ensureCoreRoom();
          const cur = typeof getCurrentRoom === 'function' ? getCurrentRoom() : null;
          if (cur) cur.historyLoaded = true;
          if (typeof renderRoomList === 'function') renderRoomList();
          if (typeof renderMessagesForCurrentRoom === 'function') renderMessagesForCurrentRoom(true);
          markAppReady();
        }
        const navigated = checkUrlRoomAndNavigate();
        const preservedRoomId = currentRoomId;
        if (!isFilePickerActive) {
          cleanUnusedEmptyRooms(preservedRoomId);
        }
        loadChatRoomsFromServer(false).then(() => {
          if (!navigated && preservedRoomId && currentRoomId !== preservedRoomId) {
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

    // iOS BFCache 복원 대응 (pageshow)
    window.addEventListener('pageshow', (e) => {
      if (e.persisted) {
        checkUrlRoomAndNavigate();
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
        if (cfgWrap && !cfgWrap.contains(e.target) && !cfgMenu.contains(e.target)) {
          closeRoomConfigPopover();
        }
      }
      const customizerMenu = document.getElementById('topbar-customizer-popover');
      if (customizerMenu && customizerMenu.classList.contains('open')) {
        const customizerWrap = document.getElementById('topbar-customizer');
        if (customizerWrap && !customizerWrap.contains(e.target) && !customizerMenu.contains(e.target)) {
          closeTopbarCustomizer();
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
    if (typeof applyTopbarVisibility === 'function') {
      applyTopbarVisibility();
    }
    if (typeof initTopbarSwipeAndScroll === 'function') {
      initTopbarSwipeAndScroll();
    }
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

  let deferredStarted = false;
  const startDeferred = () => {
    if (deferredStarted) return;
    deferredStarted = true;
    runDeferredInit();
  };

  if (typeof requestAnimationFrame === 'function' && document.visibilityState === 'visible') {
    requestAnimationFrame(() => setTimeout(startDeferred, 0));
  }
  // 백그라운드 탭 또는 rAF 쓰로틀링 방지용 하이브리드 폴백 타이머 (32ms)
  setTimeout(startDeferred, 32);
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

// ─── PWA 실행 인스턴스 딥링크 수신 (Launch Handler API & SW Message) ───
if ('launchQueue' in window && 'setConsumer' in window.launchQueue) {
  window.launchQueue.setConsumer((launchParams) => {
    if (!launchParams || !launchParams.targetURL) return;
    try {
      const url = new URL(launchParams.targetURL);
      const targetRoom = url.searchParams.get('room');
      if (targetRoom && typeof selectRoom === 'function') {
        selectRoom(targetRoom, { historyMode: 'replace' });
      }
    } catch (err) {
      console.warn('launchQueue 라우팅 처리 실패:', err);
    }
  });
}

if ('serviceWorker' in navigator) {
  navigator.serviceWorker.addEventListener('message', (event) => {
    if (event.data && event.data.type === 'NAVIGATE_ROOM') {
      const targetRoom = event.data.roomId;
      if (targetRoom && typeof selectRoom === 'function') {
        selectRoom(targetRoom, { historyMode: 'replace' });
      }
    }
  });
}
