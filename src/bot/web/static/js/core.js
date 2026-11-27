// ─── 아이콘 SVG 정의 ───
const ICONS = {
  arrowUp: '<svg class="i" viewBox="0 0 24 24"><line x1="12" y1="19" x2="12" y2="5"/><polyline points="5 12 12 5 19 12"/></svg>',
  stop: '<svg class="i" viewBox="0 0 24 24"><rect x="7" y="7" width="10" height="10" rx="2" fill="currentColor" stroke="none"/></svg>',
  mic: '<svg class="i" viewBox="0 0 24 24"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/></svg>',
  copy: '<svg class="i" viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>',
  volume: '<svg class="i" viewBox="0 0 24 24"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/></svg>',
  trash: '<svg class="i" viewBox="0 0 24 24"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/></svg>',
  chevRight: '<svg class="i chev" viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg>',
  chevUp: '<svg class="i chev" viewBox="0 0 24 24"><polyline points="18 15 12 9 6 15"/></svg>',
  chevDown: '<svg class="i chev" viewBox="0 0 24 24"><polyline points="6 9 12 15 18 9"/></svg>',
  tool: '<svg class="i" viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M12 2v3"/><path d="M12 19v3"/><path d="M4.93 4.93l2.12 2.12"/><path d="M16.95 16.95l2.12 2.12"/><path d="M2 12h3"/><path d="M19 12h3"/></svg>',
  folder: '<svg class="i" viewBox="0 0 24 24"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>',
  file: '<svg class="i" viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>',
  check: '<svg class="i" viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12"/></svg>',
  edit: '<svg class="i" viewBox="0 0 24 24"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>',
  search: '<svg class="i" viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>',
  terminal: '<svg class="i" viewBox="0 0 24 24"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>',
  globe: '<svg class="i" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>',
  link: '<svg class="i" viewBox="0 0 24 24"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>',
  externalLink: '<svg class="i ext-arrow" viewBox="0 0 24 24"><line x1="7" y1="17" x2="17" y2="7"/><polyline points="7 7 17 7 17 17"/></svg>',
  close: '<svg class="i" viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>',
  refresh: '<svg class="i" viewBox="0 0 24 24"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>',
  brain: '<svg class="i" viewBox="0 0 24 24"><path d="M9.5 2A2.5 2.5 0 0 1 12 4.5v15a2.5 2.5 0 0 1-4.96.44 2.5 2.5 0 0 1-2.96-3.08 3 3 0 0 1-.34-5.58 2.5 2.5 0 0 1 1.32-4.24 2.5 2.5 0 0 1 4.44-2.04"/><path d="M14.5 2A2.5 2.5 0 0 0 12 4.5v15a2.5 2.5 0 0 0 4.96.44 2.5 2.5 0 0 0 2.96-3.08 3 3 0 0 0 .34-5.58 2.5 2.5 0 0 0-1.32-4.24 2.5 2.5 0 0 0-4.44-2.04"/></svg>',
  spark: '<svg class="i" viewBox="0 0 16 16" stroke-width="1.5"><circle cx="8" cy="2" r="0.8"/><line x1="8" y1="2.9" x2="8" y2="5"/><rect x="2.5" y="5" width="11" height="8.5" rx="1.5"/><circle cx="6" cy="9" r="0.8"/><circle cx="10" cy="9" r="0.8"/><line x1="5.5" y1="13.5" x2="5.5" y2="15"/><line x1="10.5" y1="13.5" x2="10.5" y2="15"/></svg>',
  chat: '<svg class="i" viewBox="0 0 24 24"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>',
  shortcut: '<svg class="i" viewBox="0 0 24 24"><rect x="5" y="2" width="14" height="20" rx="3" ry="3"/><line x1="12" y1="18" x2="12.01" y2="18"/><polyline points="9 8 12 5 15 8"/><line x1="12" y1="5" x2="12" y2="13"/></svg>'
};

const CORE_ROOM_ID = '__core__';
const CORE_ROOM_TITLE = '마스터 에이전트';
const EXPERT_GENERAL_ROOM_ID = '__expert_general__';
const EXPERT_GENERAL_TITLE = '일반 대화';
const EXPERT_VAULT_ROOM_ID = '__expert_vault__';
const EXPERT_VAULT_TITLE = '세컨브레인 에이전트';
const EXPERT_CODING_ROOM_ID = '__expert_coding__';
const EXPERT_CODING_TITLE = '봇개발 에이전트';
let ORCHESTRATOR_ROOM_IDS = [
  CORE_ROOM_ID,
  EXPERT_GENERAL_ROOM_ID,
  EXPERT_VAULT_ROOM_ID,
  EXPERT_CODING_ROOM_ID,
];
let agentConfigs = [];
let editingAgentId = null;

function getAgentConfigById(agentId) {
  if (!agentId) return null;
  const clean = String(agentId).trim();
  if (Array.isArray(agentConfigs)) {
    const found = agentConfigs.find(a => a && a.id === clean);
    if (found) return found;
  }
  return null;
}

function isOrchestratorAgentRoomId(roomId) {
  if (!roomId) return false;
  const clean = String(roomId).trim();
  if (ORCHESTRATOR_ROOM_IDS.includes(clean)) return true;
  if (clean.startsWith('__custom_') && clean.endsWith('__')) return true;
  return Boolean(getAgentConfigById(clean));
}
const CORE_ORCHESTRATOR_SUGGESTIONS = [
  { tag: '전체 세션 브리핑', title: '열려 있는 모든 채팅방(AGY 세션) 요약', prompt: '현재 열려 있는 모든 채팅방(AGY 세션) 목록을 조회하고 최근 진행 상황을 한눈에 브리핑해줘.' },
  { tag: '세션 상세 열람', title: '가장 최근 채팅방 대화 내용 분석', prompt: '가장 최근에 대화한 채팅방의 내용을 읽고 핵심 결론과 남은 할 일을 정리해줘.' },
  { tag: '원격 작업 지시', title: '특정 채팅방에 후속 작업 지시하기', prompt: '현재 활성 채팅방 목록을 보여주고, 어떤 방에 후속 지시를 보낼지 추천해줘.' },
  { tag: '새 세션 스폰', title: '새 채팅방(AGY 세션) 생성 및 위임', prompt: '새 채팅방(AGY 세션)을 스폰해서 오늘의 주요 할 일과 볼트 상태를 점검하도록 작업을 위임해줘.' }
];
const VIEW_TITLES = { chat: '', vault: '볼트', automation: '자동화', settings: '설정' };
const DEFAULT_SUGGESTIONS = [
  { tag: '일정', title: '오늘 일정과 할 일 정리해줘', prompt: '오늘 캘린더 일정과 개인/업무 할 일 노트를 확인해서 우선순위별로 정리해줘.' },
  { tag: '건강 · 보험', title: '진료기록과 보험 청구 현황 점검', prompt: '최근 진료기록과 보험청구 대시보드를 대조해서 미청구 건이나 확인이 필요한 항목을 알려줘.' },
  { tag: '메모 · 드리밍', title: '스크래치패드 정리 및 요약', prompt: '스크래치패드 노트를 읽고 미분류 메모와 최근 맥락을 요약해줘.' },
  { tag: '살림 · 금융', title: '식재료 재고 또는 고정지출 확인', prompt: '식재료 관리 노트와 고정지출 현황 중 최근 업데이트된 내용을 브리핑해줘.' }
];

function isEditableElement(el) {
  return !!(el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable));
}

// ─── 디바이스 및 터치/모바일 환경 감지 (iOS, iPadOS, 태블릿 등) ───
function isTouchOrMobileDevice() {
  try {
    const ua = navigator.userAgent || '';
    // 1. 표준 모바일/태블릿 UA 감지 (iPhone, iPad, iPod, Android 등)
    if (/iPad|iPhone|iPod|Android/i.test(ua)) return true;
    // 2. iPadOS Safari 데스크톱 모드 감지 (MacIntel/Macintosh + 멀티터치)
    //    (Mac iPad 환경에서 하드웨어 키보드가 달려 있어도 navigator.maxTouchPoints > 1 로 정확히 감지됨)
    if ((navigator.platform === 'MacIntel' || ua.includes('Macintosh')) && navigator.maxTouchPoints && navigator.maxTouchPoints > 1) {
      return true;
    }
    // 3. 터치 인터페이스 미디어 쿼리 감지
    if (window.matchMedia && (window.matchMedia('(pointer: coarse)').matches || window.matchMedia('(hover: none)').matches)) {
      return true;
    }
  } catch (_) {}
  return false;
}

// ─── 보안 게이트웨이 인증 토큰 동기화 및 API fetch 헤더 자동 주입 ───
const _urlParams = new URLSearchParams(window.location.search);
const _queryToken = _urlParams.get('token');
if (_queryToken) {
  try { sessionStorage.setItem('oshyun_gateway_token', _queryToken); } catch (_) {}
}
function getGatewayToken() {
  try {
    return _queryToken || sessionStorage.getItem('oshyun_gateway_token') || '';
  } catch (_) {
    return _queryToken || '';
  }
}

const _nativeFetch = window.fetch;
window.fetch = function(url, options = {}) {
  const token = getGatewayToken();
  if (token && typeof url === 'string' && url.startsWith('/api/')) {
    options = options || {};
    options.headers = options.headers || {};
    if (options.headers instanceof Headers) {
      if (!options.headers.has('Authorization')) options.headers.set('Authorization', 'Bearer ' + token);
    } else if (Array.isArray(options.headers)) {
      if (!options.headers.some(([k]) => String(k).toLowerCase() === 'authorization')) options.headers.push(['Authorization', 'Bearer ' + token]);
    } else if (!options.headers['Authorization'] && !options.headers['authorization']) {
      options.headers['Authorization'] = 'Bearer ' + token;
    }
  }
  return _nativeFetch.call(this, url, options);
};

// ─── 클라이언트 진단 링버퍼 & 에러 원격 수집 ───
const MAX_BREADCRUMBS = 50;
const _clientBreadcrumbs = [];

function addBreadcrumb(category, message, data = null) {
  try {
    const entry = {
      t: new Date().toISOString().slice(11, 23),
      cat: String(category || 'info'),
      msg: String(message || ''),
    };
    if (data !== null && data !== undefined) {
      entry.d = typeof data === 'object' ? JSON.parse(JSON.stringify(data)) : data;
    }
    _clientBreadcrumbs.push(entry);
    if (_clientBreadcrumbs.length > MAX_BREADCRUMBS) {
      _clientBreadcrumbs.shift();
    }
  } catch (_) {}
}
window.addBreadcrumb = addBreadcrumb;
window.getClientBreadcrumbs = () => [..._clientBreadcrumbs];

function reportClientError(type, errorOrMessage, extra = {}) {
  try {
    let msg = '';
    let stack = '';
    if (errorOrMessage instanceof Error) {
      msg = errorOrMessage.message || String(errorOrMessage);
      stack = errorOrMessage.stack || '';
    } else if (typeof errorOrMessage === 'object' && errorOrMessage !== null) {
      msg = errorOrMessage.message || JSON.stringify(errorOrMessage);
      stack = errorOrMessage.stack || '';
    } else {
      msg = String(errorOrMessage || '');
    }

    addBreadcrumb('error_report', `[${type}] ${msg}`);

    const payload = {
      type: type || 'CLIENT_ERROR',
      message: msg,
      stack,
      url: window.location.href,
      userAgent: navigator.userAgent,
      currentRoomId: typeof currentRoomId !== 'undefined' ? currentRoomId : null,
      breadcrumbs: _clientBreadcrumbs.slice(-25),
      ...extra
    };

    const serialized = JSON.stringify(payload);
    if (navigator.sendBeacon) {
      const blob = new Blob([serialized], { type: 'application/json' });
      navigator.sendBeacon('/api/client-errors', blob);
    } else {
      fetch('/api/client-errors', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: serialized,
        keepalive: true
      }).catch(() => {});
    }
  } catch (_) {}
}
window.reportClientError = reportClientError;

// ─── 전역 비정상 에러 및 비동기 거부 캡처 ───
window.addEventListener('error', (event) => {
  addBreadcrumb('uncaught_error', event.message);
  reportClientError('UNCAUGHT_ERROR', event.error || event.message, {
    filename: event.filename,
    lineno: event.lineno,
    colno: event.colno
  });
});

window.addEventListener('unhandledrejection', (event) => {
  const reasonStr = event.reason ? (event.reason.message || String(event.reason)) : 'unknown';
  addBreadcrumb('unhandled_rejection', reasonStr);
  reportClientError('UNHANDLED_REJECTION', event.reason);
});

// ─── 네트워크 타임아웃 안전망 래퍼 (AbortSignal 기반) ───
function fetchWithTimeout(url, options = {}, timeoutMs = 3000) {
  const controller = new AbortController();
  const timer = setTimeout(() => {
    addBreadcrumb('net_timeout', `${url} (${timeoutMs}ms 초과)`);
    controller.abort(new Error(`Fetch timed out after ${timeoutMs}ms: ${url}`));
  }, timeoutMs);

  const originalSignal = options.signal;
  if (originalSignal) {
    if (originalSignal.aborted) {
      clearTimeout(timer);
      controller.abort(originalSignal.reason);
    } else {
      originalSignal.addEventListener('abort', () => {
        clearTimeout(timer);
        controller.abort(originalSignal.reason);
      }, { once: true });
    }
  }

  const fetchOptions = { ...options, signal: controller.signal };
  return fetch(url, fetchOptions).finally(() => {
    clearTimeout(timer);
  });
}
window.fetchWithTimeout = fetchWithTimeout;

// ─── 브라우저 히스토리 API (?room=<roomId>) 연동 ───
function buildRoomHistoryUrl(roomId) {
  try {
    const url = new URL(window.location.href);
    if (roomId) {
      url.searchParams.set('room', roomId);
    } else {
      url.searchParams.delete('room');
    }
    return url.toString();
  } catch (_) {
    return roomId ? `?room=${encodeURIComponent(roomId)}` : (window.location.pathname || '/');
  }
}

function syncRoomHistory(roomId, mode = 'push') {
  if (!roomId || mode === 'none' || !window.history) return;
  try {
    const targetUrl = buildRoomHistoryUrl(roomId);
    const currentStateRoomId = window.history.state && window.history.state.roomId;
    const currentUrlRoomId = new URLSearchParams(window.location.search).get('room');

    if (mode === 'replace') {
      if (currentStateRoomId === roomId && currentUrlRoomId === roomId) return;
      window.history.replaceState({ roomId }, '', targetUrl);
      return;
    }

    // 동일 방 중복 클릭 시 히스토리 스택이 쌓이지 않도록 방지
    if (currentStateRoomId === roomId && currentUrlRoomId === roomId) return;
    if (!currentStateRoomId && currentUrlRoomId === roomId) {
      window.history.replaceState({ roomId }, '', targetUrl);
      return;
    }
    window.history.pushState({ roomId }, '', targetUrl);
  } catch (_) {}
}

function updatePwaManifestForRoom(roomId) {
  if (!roomId) return;
  try {
    let title = '새 대화';
    if (typeof getRoomById === 'function') {
      const r = getRoomById(roomId);
      if (r && r.title) title = r.title;
    }
    if (roomId === CORE_ROOM_ID) title = '마스터 오케스트레이터';
    else if (roomId === EXPERT_GENERAL_ROOM_ID) title = '일반 대화';
    else if (roomId === EXPERT_VAULT_ROOM_ID) title = '세컨브레인 볼트';
    else if (roomId === EXPERT_CODING_ROOM_ID) title = '봇 개발';
    else if (typeof agentConfigs !== 'undefined' && Array.isArray(agentConfigs)) {
      const cfg = agentConfigs.find(a => a && a.id === roomId);
      if (cfg && cfg.name) title = cfg.name;
    }

    // 1. 브라우저 document.title 동적 갱신
    document.title = `${title} - second-bot`;

    // 2. iOS Safari 홈 화면 추가 시 타이틀 메타태그 갱신
    const appleTitleMeta = document.querySelector('meta[name="apple-mobile-web-app-title"]');
    if (appleTitleMeta) {
      appleTitleMeta.setAttribute('content', title.slice(0, 12));
    }

    // 3. PWA Web App Manifest 링크 동적 갱신 (?room=ID)
    let manifestLink = document.getElementById('pwa-manifest-link');
    if (!manifestLink) {
      manifestLink = document.querySelector('link[rel="manifest"]');
    }
    if (manifestLink) {
      const targetHref = `/manifest.json?room=${encodeURIComponent(roomId)}`;
      if (manifestLink.getAttribute('href') !== targetHref) {
        manifestLink.setAttribute('href', targetHref);
      }
    }
  } catch (err) {
    console.warn('PWA manifest 동적 갱신 실패:', err);
  }
}

// ─── 유틸리티 헬퍼 ───
function escapeHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function formatDate(ts) {
  if (!ts) return '';
  try {
    const d = typeof ts === 'number' ? (ts > 1e11 ? new Date(ts) : new Date(ts * 1000)) : new Date(ts);
    if (isNaN(d.getTime())) return String(ts);
    const m = String(d.getMonth() + 1).padStart(2, '0');
    const day = String(d.getDate()).padStart(2, '0');
    const h = String(d.getHours()).padStart(2, '0');
    const min = String(d.getMinutes()).padStart(2, '0');
    return `${m}/${day} ${h}:${min}`;
  } catch (_) {
    return String(ts);
  }
}

function parseTimestampMs(ts) {
  if (ts === null || ts === undefined || ts === '') return 0;
  if (typeof ts === 'number') {
    if (!Number.isFinite(ts) || ts <= 0) return 0;
    return ts < 1e11 ? ts * 1000 : ts;
  }
  const str = String(ts).trim();
  if (!str) return 0;
  const num = Number(str);
  if (Number.isFinite(num) && num > 0) {
    return num < 1e11 ? num * 1000 : num;
  }
  const parsed = Date.parse(str);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 0;
}

function formatMessageTime(ts) {
  const ms = parseTimestampMs(ts);
  if (!ms) return '';
  const d = new Date(ms);
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  return `${hh}:${mm}`;
}

function formatMessageFullDateTime(ts) {
  const ms = parseTimestampMs(ts);
  if (!ms) return '';
  const d = new Date(ms);
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  const ss = String(d.getSeconds()).padStart(2, '0');
  return `${y}-${m}-${day} ${hh}:${mm}:${ss}`;
}

function getMessageDateKey(ts) {
  const ms = parseTimestampMs(ts);
  if (!ms) return '';
  const d = new Date(ms);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

const WEEKDAYS_KO = ['일요일', '월요일', '화요일', '수요일', '목요일', '금요일', '토요일'];
function formatDateDividerLabel(ts) {
  const ms = parseTimestampMs(ts);
  if (!ms) return '';
  const d = new Date(ms);
  return `${d.getFullYear()}년 ${d.getMonth() + 1}월 ${d.getDate()}일 ${WEEKDAYS_KO[d.getDay()]}`;
}

function buildMsgTimeHtml(ts) {
  const t = formatMessageTime(ts);
  if (!t) return '';
  const full = formatMessageFullDateTime(ts);
  return `<span class="msg-time" title="${escapeHtml(full)}">${escapeHtml(t)}</span>`;
}

function formatRelativeTime(ts) {
  const ms = parseTimestampMs(ts);
  if (!ms) return '';
  const diffSec = Math.max(0, Math.floor((Date.now() - ms) / 1000));
  if (diffSec < 45) return '방금';
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)}분 전`;
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}시간 전`;
  if (diffSec < 172800) return '어제';
  const d = new Date(ms);
  return `${d.getMonth() + 1}/${d.getDate()}`;
}

function triggerHaptic(ms = 22) {
  if (navigator.vibrate) {
    try { navigator.vibrate(ms); } catch (_) {}
  }
}

// ─── 상태 관리 ───
let currentView = 'chat';
let socket = null;
let serverStatus = null;
let chatRooms = [];
let currentRoomId = 'room-default';
let lastRequestedRoomId = null;
const roomLiveStates = {};
let isVoiceRecording = false;
let recognition = null;
let currentEditingPath = '';
let vaultDirty = false;
let vaultTreeCache = [];
let currentAudio = null;
let currentSpeechTranscript = '';
let hasDispatchedSpeech = false;
let toastTimer = null;

// ─── 대화방 다중 선택 & 모바일 액션 상태 ───
let isRoomSelectionMode = false;
const selectedRoomIds = new Set();
let roomSearchQuery = '';
let roomServerSearchResults = null;
let roomSearchDebounceTimer = null;
let roomSearchSeq = 0;
let renamingRoomId = null;
let confirmModalCallback = null;
let suppressNextRoomClick = false;
let awaitingRoomLongPressRelease = false;
let suppressRoomClickUntil = 0;
let lastRoomLongPressTime = 0;

// ─── 로컬 스토리지 영속화 ───
const STORAGE_KEY_ROOMS = 'oshyun_chat_rooms_v1';
const STORAGE_KEY_CURRENT_ROOM = 'oshyun_current_room_id';
const STORAGE_KEY_DELETED_ROOMS = 'oshyun_deleted_rooms_v1';
const STORAGE_KEY_TTS_MODE = 'oshyun_tts_mode_v2';
const STORAGE_KEY_TTS_VOICE = 'oshyun_tts_voice_v1';
const STORAGE_KEY_STT_TIMEOUT = 'oshyun_stt_timeout_ms_v1';
const STORAGE_KEY_MOBILE_AUTOFOCUS = 'oshyun_mobile_autofocus_v1';
const STORAGE_KEY_UI_FONT_SIZE = 'oshyun_ui_font_size_v1';
const STORAGE_KEY_FONT_SIZE = 'oshyun_font_size_v1';

function getTtsMode() {
  try {
    const saved = localStorage.getItem(STORAGE_KEY_TTS_MODE);
    if (saved && ['voice_only', 'always', 'off'].includes(saved)) return saved;
  } catch (_) {}
  return (serverStatus && serverStatus.ttsMode) ? serverStatus.ttsMode : 'off';
}

function setTtsMode(mode, saveToServer = true) {
  if (!['voice_only', 'always', 'off'].includes(mode)) mode = 'off';
  try { localStorage.setItem(STORAGE_KEY_TTS_MODE, mode); } catch (_) {}
  updateTtsUi(mode);
  if (saveToServer) saveSettings();
}

function updateTtsUi(mode) {
  const current = mode || getTtsMode();
  const selectEl = document.getElementById('select-tts-mode');
  if (selectEl && selectEl.value !== current) selectEl.value = current;
}

function onTtsModeChange() {
  const selectEl = document.getElementById('select-tts-mode');
  if (selectEl) {
    setTtsMode(selectEl.value, true);
    showToast('TTS 설정이 변경되었습니다.');
  }
}

async function testTtsVoice() {
  const voiceEl = document.getElementById('select-tts-voice');
  const selectedVoice = voiceEl ? voiceEl.value : 'ko-KR-SunHiNeural';
  showToast('음성 미리듣기를 재생합니다.');
  await playTts('안녕하세요! oshyun 개인 비서입니다. 음성 합성이 정상 작동 중입니다.', selectedVoice);
}

// ─── STT 침묵 대기 시간 ───
function getSttSilenceTimeout() {
  try {
    const saved = localStorage.getItem(STORAGE_KEY_STT_TIMEOUT);
    if (saved) {
      const parsed = parseInt(saved, 10);
      if (!isNaN(parsed) && parsed >= 1000 && parsed <= 10000) return parsed;
    }
  } catch (_) {}
  return 3000;
}

function setSttSilenceTimeout(timeoutMs) {
  const val = parseInt(timeoutMs, 10) || 3000;
  try { localStorage.setItem(STORAGE_KEY_STT_TIMEOUT, String(val)); } catch (_) {}
  updateSttTimeoutUi();
}

function onSttTimeoutChange() {
  const selectEl = document.getElementById('select-stt-timeout');
  if (selectEl) {
    setSttSilenceTimeout(selectEl.value);
    const sec = (parseInt(selectEl.value, 10) / 1000).toFixed(1);
    showToast(`말 끝 감지 대기 시간이 ${sec}초로 설정되었습니다.`);
  }
}

function updateSttTimeoutUi() {
  const selectEl = document.getElementById('select-stt-timeout');
  if (selectEl) selectEl.value = String(getSttSilenceTimeout());
}

// ─── 모바일 대화창 선택 시 키보드(자동 포커스) 설정 ───
function getMobileAutofocus() {
  try {
    const saved = localStorage.getItem(STORAGE_KEY_MOBILE_AUTOFOCUS);
    if (saved !== null) return saved === 'true';
  } catch (_) {}
  return false; // 기본값: 모바일에서는 화면 가림 방지를 위해 키보드 자동 팝업 비활성화
}

function setMobileAutofocus(enabled) {
  try {
    localStorage.setItem(STORAGE_KEY_MOBILE_AUTOFOCUS, enabled ? 'true' : 'false');
  } catch (_) {}
  updateMobileAutofocusUi();
}

function updateMobileAutofocusUi() {
  const selectEl = document.getElementById('select-mobile-autofocus');
  if (selectEl) selectEl.value = getMobileAutofocus() ? 'true' : 'false';
}

function onMobileAutofocusChange() {
  const selectEl = document.getElementById('select-mobile-autofocus');
  if (selectEl) {
    const enabled = selectEl.value === 'true';
    setMobileAutofocus(enabled);
    showToast(enabled ? '모바일 대화창 선택 시 키보드가 자동으로 올라옵니다.' : '모바일 대화창 선택 시 키보드가 올라오지 않도록 설정되었습니다.');
  }
}

// ─── UI 전체 & 채팅방 본문 폰트 사이즈 설정 ───
const UI_FONT_SIZE_MIN = 12;
const UI_FONT_SIZE_MAX = 18;
const UI_FONT_SIZE_DEFAULT = 14;
const FONT_SIZE_MIN = 12;
const FONT_SIZE_MAX = 22;
const FONT_SIZE_DEFAULT = 15;
const LEGACY_FONT_SIZE_MAP = {
  small: 13.5,
  normal: 15,
  large: 16.5,
  xlarge: 18
};

function normalizeRangeFontSize(val, min, max, defVal, legacyMap = null) {
  if (legacyMap && typeof val === 'string' && Object.prototype.hasOwnProperty.call(legacyMap, val)) {
    return legacyMap[val];
  }
  const parsed = parseFloat(val);
  if (isNaN(parsed)) return defVal;
  const clamped = Math.min(max, Math.max(min, parsed));
  return Math.round(clamped * 2) / 2;
}

function normalizeUiFontSize(val) {
  return normalizeRangeFontSize(val, UI_FONT_SIZE_MIN, UI_FONT_SIZE_MAX, UI_FONT_SIZE_DEFAULT);
}

function normalizeFontSize(val) {
  return normalizeRangeFontSize(val, FONT_SIZE_MIN, FONT_SIZE_MAX, FONT_SIZE_DEFAULT, LEGACY_FONT_SIZE_MAP);
}

function formatFontSize(size) {
  const parsed = parseFloat(size);
  const val = isNaN(parsed) ? FONT_SIZE_DEFAULT : parsed;
  return Number.isInteger(val) ? `${val}px` : `${val.toFixed(1)}px`;
}

function updateFontResetAllBtnState(uiVal, chatVal) {
  const uiCur = uiVal !== undefined ? normalizeUiFontSize(uiVal) : getUiFontSize();
  const chatCur = chatVal !== undefined ? normalizeFontSize(chatVal) : getFontSize();
  const allBtn = document.getElementById('btn-all-font-size-reset');
  if (allBtn) allBtn.disabled = (uiCur === UI_FONT_SIZE_DEFAULT && chatCur === FONT_SIZE_DEFAULT);
}

function getUiFontSize() {
  try {
    const saved = localStorage.getItem(STORAGE_KEY_UI_FONT_SIZE);
    if (saved !== null && saved !== '') {
      return normalizeUiFontSize(saved);
    }
  } catch (_) {}
  return UI_FONT_SIZE_DEFAULT;
}

function applyUiFontSize(size) {
  const normalized = normalizeUiFontSize(size);
  document.documentElement.style.setProperty('--ui-font-size', `${normalized}px`);
  document.documentElement.style.setProperty('--ui-scale', String(normalized / UI_FONT_SIZE_DEFAULT));
  document.documentElement.style.setProperty('--body-font-size', `${normalized}px`);
}

function updateUiFontSizeUi(size) {
  const current = size !== undefined ? normalizeUiFontSize(size) : getUiFontSize();
  const rangeEl = document.getElementById('range-ui-font-size');
  if (rangeEl) rangeEl.value = String(current);
  const valueEl = document.getElementById('ui-font-size-value');
  if (valueEl) valueEl.textContent = formatFontSize(current);
  const resetEl = document.getElementById('btn-ui-font-size-reset');
  if (resetEl) resetEl.disabled = (current === UI_FONT_SIZE_DEFAULT);
  updateFontResetAllBtnState(current, undefined);
}

function setUiFontSize(size, persist = true) {
  const normalized = normalizeUiFontSize(size);
  if (persist) {
    try {
      localStorage.setItem(STORAGE_KEY_UI_FONT_SIZE, String(normalized));
    } catch (_) {}
  }
  applyUiFontSize(normalized);
  updateUiFontSizeUi(normalized);
}

function onUiFontSizeInput(val) {
  setUiFontSize(val, true);
}

function onUiFontSizeChange(val) {
  const rangeEl = document.getElementById('range-ui-font-size');
  const targetVal = val !== undefined ? val : (rangeEl ? rangeEl.value : UI_FONT_SIZE_DEFAULT);
  const normalized = normalizeUiFontSize(targetVal);
  setUiFontSize(normalized, true);
  showToast(`UI 전체 크기가 ${formatFontSize(normalized)}(으)로 설정되었습니다.`);
}

function resetUiFontSize() {
  setUiFontSize(UI_FONT_SIZE_DEFAULT, true);
  showToast(`UI 전체 크기가 기본값(${formatFontSize(UI_FONT_SIZE_DEFAULT)})으로 초기화되었습니다.`);
}

function getFontSize() {
  try {
    const saved = localStorage.getItem(STORAGE_KEY_FONT_SIZE);
    if (saved !== null && saved !== '') {
      const normalized = normalizeFontSize(saved);
      if (Object.prototype.hasOwnProperty.call(LEGACY_FONT_SIZE_MAP, saved)) {
        localStorage.setItem(STORAGE_KEY_FONT_SIZE, String(normalized));
      }
      return normalized;
    }
  } catch (_) {}
  return FONT_SIZE_DEFAULT;
}

function applyFontSize(size) {
  const normalized = normalizeFontSize(size);
  document.documentElement.style.setProperty('--chat-font-size', `${normalized}px`);
  document.documentElement.removeAttribute('data-font-size');
}

function setFontSize(size, persist = true) {
  const normalized = normalizeFontSize(size);
  if (persist) {
    try {
      localStorage.setItem(STORAGE_KEY_FONT_SIZE, String(normalized));
    } catch (_) {}
  }
  applyFontSize(normalized);
  updateFontSizeUi(normalized);
}

function updateFontSizeUi(size) {
  const current = size !== undefined ? normalizeFontSize(size) : getFontSize();
  const rangeEl = document.getElementById('range-font-size');
  if (rangeEl) rangeEl.value = String(current);
  const valueEl = document.getElementById('font-size-value');
  if (valueEl) valueEl.textContent = formatFontSize(current);
  const resetEl = document.getElementById('btn-font-size-reset');
  if (resetEl) resetEl.disabled = (current === FONT_SIZE_DEFAULT);
  updateFontResetAllBtnState(undefined, current);
}

function onFontSizeInput(val) {
  setFontSize(val, true);
}

function onFontSizeChange(val) {
  const rangeEl = document.getElementById('range-font-size');
  const targetVal = val !== undefined ? val : (rangeEl ? rangeEl.value : FONT_SIZE_DEFAULT);
  const normalized = normalizeFontSize(targetVal);
  setFontSize(normalized, true);
  showToast(`채팅방 본문 크기가 ${formatFontSize(normalized)}(으)로 설정되었습니다.`);
}

function resetFontSize() {
  setFontSize(FONT_SIZE_DEFAULT, true);
  showToast(`채팅방 본문 크기가 기본값(${formatFontSize(FONT_SIZE_DEFAULT)})으로 초기화되었습니다.`);
}

function resetAllFontSizes() {
  setUiFontSize(UI_FONT_SIZE_DEFAULT, true);
  setFontSize(FONT_SIZE_DEFAULT, true);
  showToast(`UI(${formatFontSize(UI_FONT_SIZE_DEFAULT)})와 채팅방 본문(${formatFontSize(FONT_SIZE_DEFAULT)}) 글자 크기가 모두 초기화되었습니다.`);
}
