// ═══ 데스크톱 좌우 사이드바 리사이저 (Resizable Splitters) ═══
(function() {
  const STORAGE_KEY_LEFT = 'oshyun_sidebar_width_v1';
  const STORAGE_KEY_RIGHT = 'oshyun_right_sidebar_width_v1';

  const DEFAULT_LEFT_W = 280;
  const MIN_LEFT_W = 220;
  const MAX_LEFT_W = 600;

  const DEFAULT_RIGHT_W = 420;
  const MIN_RIGHT_W = 320;
  const MAX_RIGHT_W = 800;

  const MIN_CENTER_W = 360;

  class SidebarResizer {
    constructor(config) {
      this.resizerId = config.resizerId;
      this.direction = config.direction; // 'left' | 'right'
      this.cssVar = config.cssVar;
      this.storageKey = config.storageKey;
      this.minWidth = config.minWidth;
      this.maxWidth = config.maxWidth;
      this.defaultWidth = config.defaultWidth;

      this.resizer = null;
      this.isDragging = false;
      this.startX = 0;
      this.startWidth = 0;
      this.currentWidth = this.defaultWidth;
      this.rafId = null;

      this.onPointerDown = this.onPointerDown.bind(this);
      this.onPointerMove = this.onPointerMove.bind(this);
      this.onPointerUp = this.onPointerUp.bind(this);
      this.onDoubleClick = this.onDoubleClick.bind(this);
      this.onKeyDown = this.onKeyDown.bind(this);
    }

    init() {
      this.resizer = document.getElementById(this.resizerId);
      if (!this.resizer) return;

      this.resizer.addEventListener('pointerdown', this.onPointerDown);
      this.resizer.addEventListener('dblclick', this.onDoubleClick);
      this.resizer.addEventListener('keydown', this.onKeyDown);
    }

    getCurrentWidth() {
      const rootVal = parseFloat(getComputedStyle(document.documentElement).getPropertyValue(this.cssVar));
      return !isNaN(rootVal) && rootVal > 0 ? rootVal : this.defaultWidth;
    }

    getOppositeWidth() {
      const oppositeVar = this.direction === 'left' ? '--right-sidebar-w' : '--sidebar-w';
      const isRightOpen = this.direction === 'left' && Boolean(document.querySelector('.master-sidebar.open, .thread-sidebar.open, .note-sidebar.open'));
      if (this.direction === 'left' && !isRightOpen) return 0;
      const val = parseFloat(getComputedStyle(document.documentElement).getPropertyValue(oppositeVar));
      return !isNaN(val) && val > 0 ? val : (this.direction === 'left' ? DEFAULT_RIGHT_W : DEFAULT_LEFT_W);
    }

    onPointerDown(e) {
      if (e.button !== 0) return; // 좌클릭만 허용
      if (window.innerWidth < 1025) return; // 데스크톱 전용

      e.preventDefault();
      this.isDragging = true;
      this.startX = e.clientX;
      this.startWidth = this.getCurrentWidth();
      this.currentWidth = this.startWidth;

      try {
        this.resizer.setPointerCapture(e.pointerId);
      } catch (_) {}

      this.resizer.classList.add('is-dragging');
      document.body.classList.add('is-resizing');

      this.resizer.addEventListener('pointermove', this.onPointerMove);
      this.resizer.addEventListener('pointerup', this.onPointerUp);
      this.resizer.addEventListener('pointercancel', this.onPointerUp);
    }

    onPointerMove(e) {
      if (!this.isDragging) return;
      e.preventDefault();

      if (this.rafId) cancelAnimationFrame(this.rafId);

      this.rafId = requestAnimationFrame(() => {
        const deltaX = e.clientX - this.startX;
        let newWidth = this.direction === 'left'
          ? this.startWidth + deltaX
          : this.startWidth - deltaX;

        // 중앙 메인 영역 최소 너비 보장
        const oppositeW = this.getOppositeWidth();
        const maxAllowed = Math.max(this.minWidth, window.innerWidth - oppositeW - MIN_CENTER_W);
        const effectiveMax = Math.min(this.maxWidth, maxAllowed);

        newWidth = Math.max(this.minWidth, Math.min(newWidth, effectiveMax));
        this.currentWidth = Math.round(newWidth);

        document.documentElement.style.setProperty(this.cssVar, `${this.currentWidth}px`);
      });
    }

    onPointerUp(e) {
      if (!this.isDragging) return;
      this.isDragging = false;

      if (this.rafId) cancelAnimationFrame(this.rafId);

      try {
        this.resizer.releasePointerCapture(e.pointerId);
      } catch (_) {}

      this.resizer.classList.remove('is-dragging');
      document.body.classList.remove('is-resizing');

      this.resizer.removeEventListener('pointermove', this.onPointerMove);
      this.resizer.removeEventListener('pointerup', this.onPointerUp);
      this.resizer.removeEventListener('pointercancel', this.onPointerUp);

      // localStorage 영속화
      if (this.currentWidth >= this.minWidth && this.currentWidth <= this.maxWidth) {
        localStorage.setItem(this.storageKey, String(this.currentWidth));
      }
    }

    onDoubleClick(e) {
      if (window.innerWidth < 1025) return;
      e.preventDefault();
      document.documentElement.style.setProperty(this.cssVar, `${this.defaultWidth}px`);
      localStorage.setItem(this.storageKey, String(this.defaultWidth));
      this.currentWidth = this.defaultWidth;
      if (typeof showToast === 'function') {
        const name = this.direction === 'left' ? '좌측 사이드바' : '우측 패널';
        showToast(`${name} 너비를 기본값(${this.defaultWidth}px)으로 복원했습니다.`);
      }
    }

    onKeyDown(e) {
      if (window.innerWidth < 1025) return;
      const step = e.shiftKey ? 25 : 10;
      let handled = false;

      if (e.key === 'ArrowLeft') {
        const delta = this.direction === 'left' ? -step : step;
        this.nudgeWidth(delta);
        handled = true;
      } else if (e.key === 'ArrowRight') {
        const delta = this.direction === 'left' ? step : -step;
        this.nudgeWidth(delta);
        handled = true;
      } else if (e.key === 'Home') {
        this.setWidth(this.minWidth);
        handled = true;
      } else if (e.key === 'End') {
        this.setWidth(this.maxWidth);
        handled = true;
      }

      if (handled) {
        e.preventDefault();
      }
    }

    nudgeWidth(delta) {
      const cur = this.getCurrentWidth();
      this.setWidth(cur + delta);
    }

    setWidth(targetWidth) {
      const clamped = Math.max(this.minWidth, Math.min(targetWidth, this.maxWidth));
      document.documentElement.style.setProperty(this.cssVar, `${clamped}px`);
      localStorage.setItem(this.storageKey, String(clamped));
      this.currentWidth = clamped;
    }
  }

  function syncRightResizerState() {
    const wrap = document.getElementById('main-views-wrap');
    const rightResizer = document.getElementById('sidebar-resizer-right');
    if (!wrap || !rightResizer) return;

    const isRightOpen = Boolean(
      document.querySelector('.master-sidebar.open, .thread-sidebar.open, .note-sidebar.open')
    );
    wrap.classList.toggle('has-right-open', isRightOpen);
    rightResizer.style.display = (isRightOpen && window.innerWidth >= 1025) ? 'flex' : 'none';
  }

  function initSidebarResizers() {
    const leftResizer = new SidebarResizer({
      resizerId: 'sidebar-resizer-left',
      direction: 'left',
      cssVar: '--sidebar-w',
      storageKey: STORAGE_KEY_LEFT,
      minWidth: MIN_LEFT_W,
      maxWidth: MAX_LEFT_W,
      defaultWidth: DEFAULT_LEFT_W
    });
    leftResizer.init();

    const rightResizer = new SidebarResizer({
      resizerId: 'sidebar-resizer-right',
      direction: 'right',
      cssVar: '--right-sidebar-w',
      storageKey: STORAGE_KEY_RIGHT,
      minWidth: MIN_RIGHT_W,
      maxWidth: MAX_RIGHT_W,
      defaultWidth: DEFAULT_RIGHT_W
    });
    rightResizer.init();

    // 우측 사이드바 열림/닫힘 변화 감지 (메모리, 스레드, 노트 뷰어 패널)
    const wrap = document.getElementById('main-views-wrap');
    if (wrap && typeof MutationObserver !== 'undefined') {
      const observer = new MutationObserver(() => {
        syncRightResizerState();
      });
      observer.observe(wrap, {
        attributes: true,
        subtree: true,
        attributeFilter: ['class', 'style']
      });
    }

    window.addEventListener('resize', syncRightResizerState);
    syncRightResizerState();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initSidebarResizers);
  } else {
    initSidebarResizers();
  }
})();
