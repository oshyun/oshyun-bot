// ─── 모바일 드로어 & 제스처 (화면 전체 스와이프 지원) ───
function openRoomDrawer() {
  cleanCurrentIfUnusedEmpty();
  document.getElementById('room-sidebar')?.classList.add('open');
  document.getElementById('drawer-backdrop')?.classList.add('open');
}

function closeRoomDrawer() {
  document.getElementById('room-sidebar')?.classList.remove('open');
  document.getElementById('drawer-backdrop')?.classList.remove('open');
}

function initMobileSwipeGestures() {
  let touchStartX = 0;
  let touchStartY = 0;
  let touchStartTime = 0;
  let isEligible = false;

  window.addEventListener('touchstart', (e) => {
    // 드로어 모드 사이드바(1024px 이하)이거나 터치 디바이스일 때 스와이프 허용
    const isEligibleDevice = window.innerWidth <= 1024 || isTouchOrMobileDevice();
    if (!e.touches || e.touches.length !== 1 || !isEligibleDevice) {
      isEligible = false;
      return;
    }

    const target = e.target;
    // 텍스트 입력창, 슬라이더, 코드 블록, 대화방 스와이프 삭제 영역에서는 드로어 스와이프 배제
    if (target && target.closest && target.closest('textarea, input, select, pre, code, .chat-input, [contenteditable="true"], .horizontal-scroll, .room-item-wrap, [data-no-swipe]')) {
      isEligible = false;
      return;
    }

    // 대화방 다중 선택 모드 시 스와이프 배제
    const sidebar = document.getElementById('room-sidebar');
    if (sidebar && sidebar.classList.contains('select-mode')) {
      isEligible = false;
      return;
    }

    touchStartX = e.touches[0].clientX;
    touchStartY = e.touches[0].clientY;
    touchStartTime = Date.now();
    isEligible = true;
  }, { passive: true });

  window.addEventListener('touchend', (e) => {
    if (!isEligible || !e.changedTouches || !e.changedTouches[0]) return;
    isEligible = false;

    const touchEndX = e.changedTouches[0].clientX;
    const touchEndY = e.changedTouches[0].clientY;
    const dx = touchEndX - touchStartX;
    const dy = Math.abs(touchEndY - touchStartY);
    const dt = Date.now() - touchStartTime;

    // 수평 스와이프 판정:
    // 1) 45px 이상 가로 이동
    // 2) 가로 이동이 세로 이동보다 1.3배 이상 지배적 (세로 스크롤 오작동 방지)
    // 3) 650ms 이내의 자연스러운 제스처
    if (Math.abs(dx) >= 45 && Math.abs(dx) > dy * 1.3 && dt < 650) {
      const sidebar = document.getElementById('room-sidebar');
      const isLeftOpen = sidebar ? sidebar.classList.contains('open') : false;
      const masterSidebar = document.getElementById('master-sidebar');
      const isRightOpen = masterSidebar ? masterSidebar.classList.contains('open') : false;
      const threadPanel = document.getElementById('thread-slide-panel');
      const isThreadOpen = threadPanel ? threadPanel.classList.contains('open') : false;
      const noteViewer = document.getElementById('note-viewer-panel');
      const isNoteOpen = noteViewer ? noteViewer.classList.contains('open') : false;

      if (isNoteOpen) {
        if (dx > 0 && typeof closeNoteViewer === 'function') closeNoteViewer();
      } else if (isThreadOpen) {
        if (dx > 0 && typeof closeThreadPanel === 'function') closeThreadPanel();
      } else if (isRightOpen) {
        // 우측 4계층 메모리 패널이 열려있을 때 오른쪽 스와이프: 닫기
        if (dx > 0) toggleMasterSidebar(false);
      } else if (isLeftOpen) {
        // 좌측 사이드바가 열려있을 때 왼쪽 스와이프: 닫기
        if (dx < 0) closeRoomDrawer();
      } else {
        // 둘 다 닫혀있을 때
        if (dx > 0) {
          // 오른쪽 스와이프: 좌측 사이드바(대화방 목록 및 메뉴) 펼치기
          openRoomDrawer();
        } else if (dx < 0) {
          // 우측 스와이프 (화면 오른쪽에서 안쪽으로 쓸어넘김): 전역 4계층 메모리 패널 열기 (모든 화면/뷰 공통)
          openMasterMemoryInspector();
        }
      }
    }
  }, { passive: true });

  window.addEventListener('touchcancel', () => {
    isEligible = false;
  }, { passive: true });
}

// ─── 모바일 당겨서 새로고침 (Pull to Refresh - 상단 당김 & 하단 당김 지원) ───
const PTR_CAPTURE_THRESHOLD_PX = 8;   // 경계 바깥 당김 시 네이티브 바운스/스크롤 조기 차단 임계값
const PTR_DEADZONE_PX = 70;           // 인디케이터가 나타나기 시작하는 최소 당김 거리 (의도치 않은 당김 방지)
const PTR_TRIGGER_THRESHOLD_PX = 165; // 새로고침이 실제로 트리거되는 당김 임계값 (충분히 당겼을 때만 작동)
const PTR_MAX_VISUAL_PX = 68;         // 인디케이터 최대 시각적 이동 한계
const PTR_DAMPING_K = 105;            // 지수 감쇠(Asymptotic Rubber-band) 저항 계수

let _ptrResetTimer = null;

function resetPullToRefreshIndicator(immediate = false) {
  const ptrIndicator = document.getElementById('ptr-indicator');
  if (!ptrIndicator) return;
  if (_ptrResetTimer) {
    clearTimeout(_ptrResetTimer);
    _ptrResetTimer = null;
  }
  ptrIndicator.classList.remove('pulling', 'ready', 'refreshing');
  if (immediate) {
    ptrIndicator.classList.remove('returning', 'from-bottom');
    ptrIndicator.style.transition = 'none';
    ptrIndicator.style.transform = '';
    ptrIndicator.style.opacity = '';
    ptrIndicator.style.top = '';
    ptrIndicator.style.bottom = '';
    // 강제 리플로우 후 인라인 transition 제거하여 CSS 기본 트랜지션 복원
    void ptrIndicator.offsetHeight;
    ptrIndicator.style.transition = '';
    return;
  }
  ptrIndicator.classList.add('returning');
  ptrIndicator.style.transform = '';
  ptrIndicator.style.opacity = '';
  _ptrResetTimer = setTimeout(() => {
    _ptrResetTimer = null;
    ptrIndicator.classList.remove('returning', 'from-bottom');
    ptrIndicator.style.top = '';
    ptrIndicator.style.bottom = '';
  }, 240);
}

function initPullToRefresh() {
  const ptrIndicator = document.getElementById('ptr-indicator');
  const ptrText = document.getElementById('ptr-text');
  if (!ptrIndicator || !ptrText) return;

  let startY = 0;
  let startX = 0;
  let isAtTop = false;
  let isAtBottom = false;
  let canScrollUp = false;
  let canScrollDown = false;
  let isEligible = false;
  let isPulling = false;
  let pullDirection = null; // 'top' | 'bottom' | null
  let activeScroller = null;

  function getActiveScrollable(touchTarget) {
    // 1) 터치 대상에서 가장 가까운 실제 스크롤 가능 조상 우선 탐색 (모든 페이지·탭 공통 지원)
    let el = touchTarget && touchTarget.nodeType === 1 ? touchTarget : (touchTarget && touchTarget.parentElement ? touchTarget.parentElement : null);
    while (el && el !== document.body && el !== document.documentElement) {
      if (el.scrollHeight > el.clientHeight + 1) {
        const ov = window.getComputedStyle(el).overflowY;
        if (ov === 'auto' || ov === 'scroll' || ov === 'overlay') {
          return el;
        }
      }
      el = el.parentElement;
    }
    if (currentView === 'chat') {
      return document.getElementById('messages-container');
    }
    if (currentView === 'vault') {
      return document.querySelector('#view-vault .vault-editor-body') ||
             document.querySelector('#view-vault .vault-tree') ||
             document.getElementById('view-vault');
    }
    const activeView = document.getElementById('view-' + currentView) || document.querySelector('.view.active');
    if (activeView) {
      return activeView.querySelector('.page-scroll') || activeView;
    }
    return document.querySelector('.main');
  }

  function getScrollTop(scroller) {
    if (!scroller) return 0;
    return scroller.scrollTop !== undefined ? scroller.scrollTop : (window.scrollY || document.documentElement.scrollTop || 0);
  }

  function getDistFromBottom(scroller) {
    if (!scroller) return 0;
    if (scroller.scrollHeight !== undefined && scroller.clientHeight !== undefined) {
      return Math.max(0, scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight);
    }
    const docH = document.documentElement.scrollHeight;
    const winH = window.innerHeight;
    const scrollY = window.scrollY || document.documentElement.scrollTop || 0;
    return Math.max(0, docH - scrollY - winH);
  }

  function getBottomOffset() {
    if (currentView === 'chat') {
      const comp = document.querySelector('.composer-wrap');
      const compH = comp ? comp.offsetHeight : 0;
      return compH + 12;
    }
    return 16;
  }

  function calcDampedPullDist(effectiveDy) {
    if (effectiveDy <= 0) return 0;
    return PTR_MAX_VISUAL_PX * (1 - Math.exp(-effectiveDy / PTR_DAMPING_K));
  }

  function restoreScrollerAnchor() {
    if (!activeScroller || !pullDirection) return;
    if (pullDirection === 'top' && activeScroller.scrollTop !== undefined && activeScroller.scrollTop < 2) {
      activeScroller.scrollTop = 0;
    } else if (pullDirection === 'bottom' && activeScroller.scrollTop !== undefined) {
      const maxTop = Math.max(0, activeScroller.scrollHeight - activeScroller.clientHeight);
      if (maxTop - activeScroller.scrollTop < 2) {
        activeScroller.scrollTop = maxTop;
      }
    }
  }

  window.addEventListener('touchstart', (e) => {
    // 단일 터치 제스처일 때 데스크톱/태블릿/모바일 크기와 무관하게 당겨서 새로고침(PTR) 상시 유지
    if (!e.touches || e.touches.length !== 1) {
      isEligible = false;
      return;
    }

    // 이전 복귀 애니메이션이 진행 중이었다면 즉시 정지 및 클린업하여 타이머/트랜스폼 경합 원천 방지
    if (_ptrResetTimer || ptrIndicator.classList.contains('returning')) {
      resetPullToRefreshIndicator(true);
    }

    // 모바일 사이드바 드로어가 열려 있거나 다중 선택 모드이면 무시
    const sidebar = document.getElementById('room-sidebar');
    const masterSidebar = document.getElementById('master-sidebar');
    if ((sidebar && (sidebar.classList.contains('open') || sidebar.classList.contains('select-mode'))) ||
        (window.innerWidth <= 1024 && masterSidebar && masterSidebar.classList.contains('open'))) {
      isEligible = false;
      return;
    }

    // 입력창, 모달, 버튼, 사이드바 등 인터랙티브 영역 터치 시 무시
    const target = e.target;
    if (target && target.closest && target.closest('textarea, input, select, button, .icon-btn, .btn, .modal-backdrop, #room-sidebar, .sidebar, #master-sidebar, .master-sidebar, .master-drawer-backdrop')) {
      isEligible = false;
      return;
    }

    // 유효한 활성 스크롤러가 없으면 무시
    const scroller = getActiveScrollable(target);
    if (!scroller) {
      isEligible = false;
      return;
    }

    const scrollTop = getScrollTop(scroller);
    const distFromBottom = getDistFromBottom(scroller);

    // 스크롤 가능한 추가 컨텐츠 존재 여부 (2px 오차 허용)
    canScrollUp = scrollTop > 2;
    canScrollDown = distFromBottom > 2;

    // 최상단이거나 최하단인 경우 PTR 제스처 후보
    // (스크롤할 컨텐츠가 없는 짧은 방이나 빈 화면은 canScrollUp=false, canScrollDown=false 이므로 isAtTop, isAtBottom 모두 true)
    isAtTop = !canScrollUp;
    isAtBottom = !canScrollDown;

    if (!isAtTop && !isAtBottom) {
      isEligible = false;
      return;
    }

    activeScroller = scroller;
    startY = e.touches[0].clientY;
    startX = e.touches[0].clientX;
    isEligible = true;
    isPulling = false;
    pullDirection = null;
  }, { passive: true });

  function cancelPull() {
    isEligible = false;
    if (isPulling || pullDirection) {
      isPulling = false;
      resetPullToRefreshIndicator(false);
    }
    pullDirection = null;
    activeScroller = null;
  }

  window.addEventListener('touchmove', (e) => {
    if (!isEligible || !e.touches || !e.touches[0]) return;

    const dy = e.touches[0].clientY - startY;
    const dx = Math.abs(e.touches[0].clientX - startX);

    // 수평 스와이프 제스처인 경우 PTR 취소하여 사이드바 등 정상 스와이프 보장
    if (!pullDirection && dx > Math.abs(dy)) {
      cancelPull();
      return;
    }

    // 실제로 스크롤할 내용이 남아있는 방향으로 일반 스크롤을 시도할 때만 PTR 취소 (방향 확정 전에만!)
    // - 아래로 스크롤할 내용이 남아있는데 손가락을 위로 밀 때 (dy < 0)
    // - 위로 스크롤할 내용이 남아있는데 손가락을 아래로 내릴 때 (dy > 0)
    if (!pullDirection && ((canScrollDown && dy < 0) || (canScrollUp && dy > 0))) {
      cancelPull();
      return;
    }

    // 방향 조기 선점: 경계(최상단/최하단)에서 바깥 방향으로 PTR_CAPTURE_THRESHOLD_PX(8px) 이상 수직 이동 시
    // 네이티브 브라우저 오버스크롤/러버밴드가 시작되기 전에 방향을 확정하고 e.preventDefault()로 스크롤 튐을 원천 차단
    if (!pullDirection) {
      if (isAtTop && dy > PTR_CAPTURE_THRESHOLD_PX && dy > dx * 1.5) {
        pullDirection = 'top';
        ptrIndicator.classList.remove('from-bottom', 'returning');
        ptrIndicator.style.top = '';
        ptrIndicator.style.bottom = '';
      } else if (isAtBottom && dy < -PTR_CAPTURE_THRESHOLD_PX && -dy > dx * 1.5) {
        pullDirection = 'bottom';
        ptrIndicator.classList.remove('returning');
        ptrIndicator.classList.add('from-bottom');
        ptrIndicator.style.bottom = `${getBottomOffset()}px`;
        ptrIndicator.style.top = 'auto';
      } else {
        return;
      }
    }

    // PTR 방향이 확정된 즉시 브라우저 기본 스크롤/바운스를 차단하고 스크롤 앵커를 고정
    if (e.cancelable) e.preventDefault();
    restoreScrollerAnchor();

    // 상단/하단 당김 중 처리 (시각적 노출은 PTR_DEADZONE_PX 이상 당겼을 때부터만 시작)
    const isBottom = pullDirection === 'bottom';
    const signedDy = isBottom ? -dy : dy;
    if (signedDy <= PTR_DEADZONE_PX) {
      if (isPulling) {
        isPulling = false;
        ptrIndicator.classList.remove('pulling', 'ready');
        ptrIndicator.style.transform = '';
        ptrIndicator.style.opacity = '';
      }
      return;
    }

    isPulling = true;
    ptrIndicator.classList.add('pulling');
    const effectiveDy = signedDy - PTR_DEADZONE_PX;
    const pullDist = calcDampedPullDist(effectiveDy);
    const offsetY = isBottom ? (50 - pullDist) : (pullDist - 50);
    ptrIndicator.style.transform = `translate3d(0, ${offsetY}px, 0)`;
    ptrIndicator.style.opacity = String(Math.min(effectiveDy / 45, 1));

    if (signedDy >= PTR_TRIGGER_THRESHOLD_PX) {
      ptrIndicator.classList.add('ready');
      ptrText.innerText = '놓아서 새로고침';
    } else {
      ptrIndicator.classList.remove('ready');
      ptrText.innerText = isBottom ? '올려서 새로고침' : '당겨서 새로고침';
    }
  }, { passive: false });

  function finishPull() {
    if (!isEligible && !isPulling && !pullDirection) return;
    isEligible = false;

    if (pullDirection) {
      const wasReady = isPulling && ptrIndicator.classList.contains('ready');
      isPulling = false;
      ptrIndicator.classList.remove('pulling');
      restoreScrollerAnchor();

      if (wasReady) {
        ptrIndicator.classList.remove('ready', 'returning');
        ptrIndicator.classList.add('refreshing');
        ptrText.innerText = '새로고치는 중…';

        if (pullDirection === 'bottom') {
          ptrIndicator.style.transform = 'translate3d(0, -12px, 0)';
        } else {
          ptrIndicator.style.transform = 'translate3d(0, 12px, 0)';
        }
        ptrIndicator.style.opacity = '1';

        if (navigator.vibrate) {
          try { navigator.vibrate(15); } catch (_) {}
        }

        setTimeout(() => {
          window.location.reload();
        }, 260);
      } else {
        // 취소 또는 임계값 미달 복귀 시: returning 트랜지션을 활성화하여 잔존 없이 부드럽게 원복
        resetPullToRefreshIndicator(false);
      }
    }
    pullDirection = null;
    activeScroller = null;
  }

  window.addEventListener('touchend', finishPull, { passive: true });
  window.addEventListener('touchcancel', finishPull, { passive: true });
}

// ─── 모바일 핀치 줌인/줌아웃 채팅방 본문 글자 크기 실시간 조절 ───
function initPinchZoomFontSize() {
  const hud = document.getElementById('pinch-font-hud');
  const hudSize = document.getElementById('pinch-hud-size');
  const hudBadge = document.getElementById('pinch-hud-badge');
  const hudFill = document.getElementById('pinch-hud-fill');

  let isPinching = false;
  let pinchStartDist = 0;
  let pinchStartFontSize = FONT_SIZE_DEFAULT;
  let pinchCurrentFontSize = FONT_SIZE_DEFAULT;
  let pinchRafId = null;
  let hudHideTimer = null;
  let hasMovedSignificantly = false;

  function getTouchDistance(t1, t2) {
    const dx = t1.clientX - t2.clientX;
    const dy = t1.clientY - t2.clientY;
    return Math.hypot(dx, dy);
  }

  function updateHud(size, isLimitMin, isLimitMax) {
    if (!hud) return;
    const rounded = Math.round(size * 2) / 2;
    if (hudSize) hudSize.textContent = formatFontSize(rounded);

    if (hudFill) {
      const pct = Math.max(0, Math.min(100, ((size - FONT_SIZE_MIN) / (FONT_SIZE_MAX - FONT_SIZE_MIN)) * 100));
      hudFill.style.width = pct + '%';
    }

    if (hudBadge) {
      if (isLimitMin) {
        hudBadge.textContent = '최소';
        hudBadge.className = 'pinch-hud-badge is-limit';
      } else if (isLimitMax) {
        hudBadge.textContent = '최대';
        hudBadge.className = 'pinch-hud-badge is-limit';
      } else if (rounded === FONT_SIZE_DEFAULT) {
        hudBadge.textContent = '기본값';
        hudBadge.className = 'pinch-hud-badge is-default';
      } else {
        hudBadge.textContent = '';
        hudBadge.className = 'pinch-hud-badge';
      }
    }
  }

  function showHud(size) {
    if (!hud) return;
    if (hudHideTimer) {
      clearTimeout(hudHideTimer);
      hudHideTimer = null;
    }
    updateHud(size, size <= FONT_SIZE_MIN, size >= FONT_SIZE_MAX);
    hud.classList.add('visible');
  }

  function hideHud() {
    if (!hud) return;
    if (hudHideTimer) clearTimeout(hudHideTimer);
    hudHideTimer = setTimeout(() => {
      hud.classList.remove('visible');
    }, 750);
  }

  window.addEventListener('touchstart', (e) => {
    if (!e.touches || e.touches.length !== 2) return;
    if (currentView !== 'chat') return;

    // 모달이나 사이드바가 열려 있으면 핀치 무시
    const sidebar = document.getElementById('room-sidebar');
    const masterSidebar = document.getElementById('master-sidebar');
    const modalBackdrop = document.querySelector('.modal-backdrop.open');
    if (modalBackdrop ||
        (sidebar && sidebar.classList.contains('open')) ||
        (masterSidebar && masterSidebar.classList.contains('open'))) {
      return;
    }

    // 대화창 영역 내부 터치인지 확인
    const target = e.target;
    if (!target || !target.closest || !target.closest('#chat-main-area, #messages-container, #view-chat')) {
      return;
    }

    const dist = getTouchDistance(e.touches[0], e.touches[1]);
    if (dist < 15) return;

    isPinching = true;
    hasMovedSignificantly = false;
    pinchStartDist = dist;
    pinchStartFontSize = getFontSize();
    pinchCurrentFontSize = pinchStartFontSize;

    document.body.classList.add('is-pinching-font');

    if (window.getSelection) {
      try { window.getSelection().removeAllRanges(); } catch (_) {}
    }

    if (e.cancelable) e.preventDefault();
    showHud(pinchCurrentFontSize);
  }, { passive: false });

  window.addEventListener('touchmove', (e) => {
    if (!isPinching) return;
    if (!e.touches || e.touches.length < 2) return;

    if (e.cancelable) e.preventDefault();

    const curDist = getTouchDistance(e.touches[0], e.touches[1]);
    if (Math.abs(curDist - pinchStartDist) > 5) {
      hasMovedSignificantly = true;
    }

    // 거리 비율에 댐핑 계수(0.85)를 적용하여 부드럽고 정밀한 실시간 조절
    const scale = curDist / pinchStartDist;
    const factor = 1 + (scale - 1) * 0.85;
    let targetSize = pinchStartFontSize * factor;
    targetSize = Math.min(FONT_SIZE_MAX, Math.max(FONT_SIZE_MIN, targetSize));
    pinchCurrentFontSize = targetSize;

    if (!pinchRafId) {
      pinchRafId = requestAnimationFrame(() => {
        pinchRafId = null;
        applyFontSize(pinchCurrentFontSize);
        const rounded = Math.round(pinchCurrentFontSize * 2) / 2;
        updateFontSizeUi(rounded);
        updateHud(pinchCurrentFontSize, pinchCurrentFontSize <= FONT_SIZE_MIN, pinchCurrentFontSize >= FONT_SIZE_MAX);
      });
    }
  }, { passive: false });

  function endPinch() {
    if (!isPinching) return;
    isPinching = false;
    document.body.classList.remove('is-pinching-font');

    if (pinchRafId) {
      cancelAnimationFrame(pinchRafId);
      pinchRafId = null;
    }

    if (hasMovedSignificantly) {
      const finalSize = normalizeFontSize(pinchCurrentFontSize);
      setFontSize(finalSize, true);

      if (typeof navigator !== 'undefined' && navigator.vibrate) {
        try { navigator.vibrate(12); } catch (_) {}
      }
    }

    hideHud();
  }

  window.addEventListener('touchend', (e) => {
    if (isPinching && (!e.touches || e.touches.length < 2)) {
      endPinch();
    }
  }, { passive: true });

  window.addEventListener('touchcancel', () => {
    if (isPinching) endPinch();
  }, { passive: true });

  // iOS Safari gesturestart/change/end 방지 (전체 뷰포트 확대 차단)
  const preventSafariGesture = (e) => {
    if (currentView === 'chat' && e.cancelable) {
      e.preventDefault();
    }
  };
  document.addEventListener('gesturestart', preventSafariGesture, { passive: false });
  document.addEventListener('gesturechange', preventSafariGesture, { passive: false });
  document.addEventListener('gestureend', preventSafariGesture, { passive: false });
}

function initVisualViewportHandler() {
  const updateViewportHeight = () => {
    if (window.visualViewport) {
      const h = window.visualViewport.height;
      document.documentElement.style.setProperty('--app-height', `${h}px`);
      const isKbOpen = (window.innerWidth <= 1024 || isTouchOrMobileDevice()) && h < window.innerHeight * 0.85;
      document.body.classList.toggle('kb-open', isKbOpen);
      if (isKbOpen && currentView === 'chat') {
        scrollToBottomIfNear(true);
      }
      if (window.visualViewport.offsetTop > 0 || window.scrollY !== 0 || window.scrollX !== 0) {
        window.scrollTo(0, 0);
      }
    } else {
      document.documentElement.style.setProperty('--app-height', `${window.innerHeight}px`);
    }
  };

  if (window.visualViewport) {
    window.visualViewport.addEventListener('resize', updateViewportHeight);
    window.visualViewport.addEventListener('scroll', updateViewportHeight);
  }
  window.addEventListener('resize', updateViewportHeight);
  updateViewportHeight();

  // 키보드/단축키 바 닫힘 시(focusout) Safari 하단 잔여 흰색 공백 버그 강제 치유 및 레이아웃 재계산
  document.addEventListener('focusout', (e) => {
    if (isEditableElement(e.target)) {
      setTimeout(() => {
        window.scrollTo(0, 0);
        updateViewportHeight();
        window.dispatchEvent(new Event('resize'));
      }, 80);
    }
  });
}

// ─── 메시지 말풍선 롱프레스 (브라우저 기본 텍스트 선택 보존) ───
function initMessageLongPressCopy() {
  if (typeof bindMessageLongPressEvents === 'function') {
    bindMessageLongPressEvents();
  }
}
