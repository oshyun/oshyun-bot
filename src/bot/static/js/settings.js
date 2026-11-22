// ─── 상태 및 설정 ───
function applyStatusPayload(data) {
  if (!data) return;
  serverStatus = data;

  if (Array.isArray(data.agents) && typeof syncAgentConfigsState === 'function') {
    syncAgentConfigsState(data.agents, data.orchestratorRoomIds);
  }

  const agentNameEl = document.getElementById('avatar-agent-name');
  if (agentNameEl) {
    agentNameEl.innerText = data.engineLabel || 'AGY';
  }

  const selEngine = document.getElementById('select-engine');
  if (selEngine && data.engine) selEngine.value = data.engine;

  const selModel = document.getElementById('select-model');
  if (selModel && Array.isArray(data.availableModels)) {
    selModel.innerHTML = data.availableModels.map(m =>
      `<option value="${escapeHtml(m.id)}" ${m.id === data.model ? 'selected' : ''}>${escapeHtml(m.label)}</option>`
    ).join('');
  }

  const selEffort = document.getElementById('select-effort');
  if (selEffort) {
    if (Array.isArray(data.supportedEfforts) && data.supportedEfforts.length > 0) {
      selEffort.disabled = false;
      selEffort.innerHTML = data.supportedEfforts.map(eff => {
        const labels = { low: 'Low · 빠른 응답', medium: 'Medium · 균형', high: 'High · 심층 추론', max: 'Max · 최대 추론' };
        return `<option value="${escapeHtml(eff)}" ${eff === (data.effort || 'medium') ? 'selected' : ''}>${labels[eff] || eff}</option>`;
      }).join('');
    } else if (Array.isArray(data.supportedEfforts) && data.supportedEfforts.length === 0) {
      selEffort.innerHTML = `<option value="">미지원 (기본값)</option>`;
      selEffort.disabled = true;
    } else if (data.effort) {
      selEffort.disabled = false;
      selEffort.value = data.effort;
    }
  }

  const selTts = document.getElementById('select-tts-voice');
  let savedVoice = null;
  try { savedVoice = localStorage.getItem(STORAGE_KEY_TTS_VOICE); } catch (_) {}
  const targetVoice = savedVoice || data.ttsVoice || data.defaultTtsVoice;
  if (selTts && Array.isArray(data.ttsVoices)) {
    selTts.innerHTML = data.ttsVoices.map(v =>
      `<option value="${escapeHtml(v.id)}" ${v.id === targetVoice ? 'selected' : ''}>${escapeHtml(v.label)}</option>`
    ).join('');
  }

  updateTtsUi();
  updateMobileAutofocusUi();
  updateUiFontSizeUi();
  updateFontSizeUi();

  const clientIpEl = document.getElementById('client-ip-display');
  if (clientIpEl && data.clientIp) clientIpEl.innerText = data.clientIp;
  const clientMacEl = document.getElementById('client-mac-display');
  if (clientMacEl && data.clientMac) clientMacEl.innerText = data.clientMac;
  const clientDescEl = document.getElementById('client-ip-desc');
  if (clientDescEl && data.ipDescription) clientDescEl.innerText = data.ipDescription;
  const loopbackNotice = document.getElementById('nat-loopback-notice');
  if (loopbackNotice) loopbackNotice.style.display = data.ipType === 'nat_loopback' ? 'block' : 'none';

  if (Array.isArray(data.activeRuns)) {
    applyActiveRunsSnapshot(data.activeRuns, true);
  }
  if ('usage' in data) {
    updateAvatarUsageBadge(data.usage);
  }
  updateRoomConfigBar();
}

async function loadStatusAndSettings() {
  try {
    const res = await fetch('/api/status');
    applyStatusPayload(await res.json());
  } catch (_) {}
  if (typeof loadGoogleAuthStatus === 'function') {
    loadGoogleAuthStatus();
  }
}

async function onSettingEngineChange() {
  const newEngine = document.getElementById('select-engine')?.value;
  if (!newEngine) return;
  try {
    const res = await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ engine: newEngine })
    });
    const data = await res.json();
    if (data.ok || data.status === 'ok') {
      showToast(`엔진이 ${data.engineLabel || newEngine}(으)로 변경되었습니다.`);
      applyStatusPayload(data);
    }
  } catch (err) {
    console.error('엔진 변경 오류:', err);
  }
}

async function saveSettings() {
  const ttsVoice = document.getElementById('select-tts-voice')?.value || '';
  const ttsMode = getTtsMode();
  if (ttsVoice) {
    try { localStorage.setItem(STORAGE_KEY_TTS_VOICE, ttsVoice); } catch (_) {}
  }
  const payload = {
    engine: document.getElementById('select-engine')?.value,
    model: document.getElementById('select-model')?.value,
    effort: document.getElementById('select-effort')?.value,
    ttsVoice,
    ttsMode
  };
  try {
    const res = await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const data = await res.json();
    if (data.ok) {
      showToast('설정이 저장되었습니다.');
      loadStatusAndSettings();
    }
  } catch (_) {}
}

// ─── 브라우저 / 시스템 알림 (Notification API & Service Worker) ───
const STORAGE_KEY_NOTIFICATION_MODE = 'oshyun_notification_mode'; // 'background' | 'always' | 'off'
const STORAGE_KEY_NOTIFICATION_SOUND = 'oshyun_notification_sound'; // 'true' | 'false'
let hasAutoPromptedNotification = false;

function getNotificationMode() {
  try {
    const val = localStorage.getItem(STORAGE_KEY_NOTIFICATION_MODE);
    if (val === 'background' || val === 'always' || val === 'off') return val;
  } catch (_) {}
  return 'background';
}

function isNotificationSoundEnabled() {
  try {
    const val = localStorage.getItem(STORAGE_KEY_NOTIFICATION_SOUND);
    if (val !== null) return val === 'true';
  } catch (_) {}
  return true;
}

function cleanNotificationText(raw) {
  if (!raw) return '답변이 완료되었습니다.';
  let text = String(raw);
  text = text.replace(/```[\s\S]*?```/g, ' [코드] ');
  text = text.replace(/`([^`]+)`/g, '$1');
  text = text.replace(/\[link:([^\]]+)\]\([^)]+\)/g, '$1');
  text = text.replace(/\[([^\]]+)\]\([^)]+\)/g, '$1');
  text = text.replace(/\[\[(?:[^|\]]*\|)?([^\]]+)\]\]/g, '$1');
  text = text.replace(/^[#>\-*+]\s+/gm, '');
  text = text.replace(/[*_~]{1,3}([^*_~]+)[*_~]{1,3}/g, '$1');
  text = text.replace(/<[^>]+>/g, '');
  text = text.replace(/\s+/g, ' ').trim();
  if (!text) return '답변이 완료되었습니다.';
  return text.length > 120 ? text.slice(0, 117) + '…' : text;
}

function playNotificationChime() {
  if (!isNotificationSoundEnabled()) return;
  try {
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (!AudioCtx) return;
    const ctx = new AudioCtx();
    const now = ctx.currentTime;

    // 1st note (587.33 Hz - D5)
    const osc1 = ctx.createOscillator();
    const gain1 = ctx.createGain();
    osc1.type = 'sine';
    osc1.frequency.setValueAtTime(587.33, now);
    gain1.gain.setValueAtTime(0, now);
    gain1.gain.linearRampToValueAtTime(0.12, now + 0.02);
    gain1.gain.exponentialRampToValueAtTime(0.001, now + 0.22);
    osc1.connect(gain1);
    gain1.connect(ctx.destination);
    osc1.start(now);
    osc1.stop(now + 0.22);

    // 2nd note (880 Hz - A5)
    const osc2 = ctx.createOscillator();
    const gain2 = ctx.createGain();
    osc2.type = 'sine';
    osc2.frequency.setValueAtTime(880, now + 0.1);
    gain2.gain.setValueAtTime(0, now + 0.1);
    gain2.gain.linearRampToValueAtTime(0.16, now + 0.12);
    gain2.gain.exponentialRampToValueAtTime(0.001, now + 0.4);
    osc2.connect(gain2);
    gain2.connect(ctx.destination);
    osc2.start(now + 0.1);
    osc2.stop(now + 0.4);

    setTimeout(() => { ctx.close().catch(() => {}); }, 800);
  } catch (_) {}
}

async function triggerSystemNotification({ title, body, roomId }) {
  if (!('Notification' in window)) return false;
  if (Notification.permission !== 'granted') return false;

  const options = {
    body: body || '답변이 도착했습니다.',
    icon: '/icons/icon-192.png',
    badge: '/favicon.svg',
    tag: `room-${roomId || 'general'}`,
    renotify: true,
    data: {
      roomId: roomId || '',
      url: roomId ? `/?room=${encodeURIComponent(roomId)}` : '/'
    }
  };

  // 1. Service Worker 등록 확인 (PWA / 백그라운드 우선)
  try {
    if ('serviceWorker' in navigator) {
      const reg = await navigator.serviceWorker.ready;
      if (reg && typeof reg.showNotification === 'function') {
        await reg.showNotification(title, options);
        return true;
      }
    }
  } catch (err) {
    console.warn('[Notification] SW showNotification 실패, window.Notification 폴백:', err);
  }

  // 2. 일반 Notification API 폴백
  try {
    const notif = new Notification(title, options);
    notif.onclick = function () {
      try {
        window.focus();
        if (roomId && typeof selectRoom === 'function') {
          selectRoom(roomId);
        }
      } catch (_) {}
      notif.close();
    };
    return true;
  } catch (err) {
    console.warn('[Notification] Notification 인스턴스 생성 실패:', err);
    return false;
  }
}

function handleAgentResponseNotification(targetRoom, replyText, opts = {}) {
  const mode = getNotificationMode();
  if (mode === 'off') return;
  if (opts && opts.silent) return;

  // 하위 에이전트 방이거나 바이패스된 응답은 마스터 방과 중복 알림을 방지하기 위해 침묵
  if (targetRoom && (targetRoom.isOrchestratorChild || targetRoom.parentAgentId)) {
    return;
  }

  const isCurrentRoom = targetRoom && (targetRoom.id === currentRoomId);
  const isHidden = document.hidden || (document.visibilityState === 'hidden') || !document.hasFocus();

  // 'always'이거나, 'background' 상태(문서가 가려졌거나 포커스가 없거나 다른 대화방일 때)
  const shouldNotify = (mode === 'always') || (mode === 'background' && (isHidden || !isCurrentRoom));

  if (!shouldNotify) return;

  const roomTitle = (targetRoom && targetRoom.title) ? targetRoom.title : '대화방';
  const cleanBody = cleanNotificationText(replyText);
  const isError = Boolean(opts && opts.isError);
  const title = isError ? `[${roomTitle}] 실행 오류` : `[${roomTitle}] 답변 완료`;

  // 브라우저/기기 알림 발송
  triggerSystemNotification({
    title,
    body: cleanBody,
    roomId: targetRoom ? targetRoom.id : ''
  });

  // 알림음 재생
  playNotificationChime();
}

function maybeRequestNotificationPermission() {
  if (!('Notification' in window)) return;
  if (Notification.permission !== 'default') return;
  if (getNotificationMode() === 'off') return;
  if (hasAutoPromptedNotification) return;

  hasAutoPromptedNotification = true;
  try {
    Notification.requestPermission().then(() => {
      updateNotificationUi();
    }).catch(() => {});
  } catch (_) {}
}

async function requestNotificationPermissionManual() {
  if (!('Notification' in window)) {
    showToast('이 브라우저는 웹 알림(Notification API)을 지원하지 않습니다.');
    return;
  }
  try {
    const perm = await Notification.requestPermission();
    updateNotificationUi();
    if (perm === 'granted') {
      showToast('브라우저 알림 권한이 허용되었습니다.');
      triggerSystemNotification({
        title: '알림 설정 완료',
        body: '답변이 완료되면 이와 같이 브라우저/시스템 알림이 표시됩니다.',
        roomId: currentRoomId
      });
      playNotificationChime();
    } else if (perm === 'denied') {
      showToast('알림 권한이 차단되었습니다. 브라우저 사이트 설정에서 허용해 주세요.');
    }
  } catch (err) {
    showToast('알림 권한 요청 중 오류: ' + (err.message || String(err)));
  }
}

function testNotificationManual() {
  if (!('Notification' in window)) {
    showToast('이 브라우저는 웹 알림을 지원하지 않습니다.');
    return;
  }
  if (Notification.permission !== 'granted') {
    showToast('먼저 [권한 허용] 버튼을 눌러 알림을 허용해 주세요.');
    return;
  }
  const currentRoom = (typeof getRoomById === 'function') ? getRoomById(currentRoomId) : null;
  const roomTitle = currentRoom ? currentRoom.title : '테스트 대화방';
  triggerSystemNotification({
    title: `[${roomTitle}] 답변 완료 (테스트)`,
    body: '알림이 정상적으로 수신되었습니다. 에이전트 응답 시 이와 같이 안내됩니다.',
    roomId: currentRoomId
  });
  playNotificationChime();
  showToast('테스트 알림을 발송했습니다.');
}

function updateNotificationUi() {
  const permDesc = document.getElementById('notification-permission-desc');
  const btnReq = document.getElementById('btn-request-notification');
  const selMode = document.getElementById('select-notification-mode');
  const selSound = document.getElementById('select-notification-sound');

  if (selMode) selMode.value = getNotificationMode();
  if (selSound) selSound.value = isNotificationSoundEnabled() ? 'true' : 'false';

  if (!('Notification' in window)) {
    if (permDesc) permDesc.innerHTML = '<span style="color:var(--text-3);">이 브라우저는 알림을 지원하지 않습니다.</span>';
    if (btnReq) btnReq.style.display = 'none';
    return;
  }

  const perm = Notification.permission;
  if (perm === 'granted') {
    if (permDesc) permDesc.innerHTML = '<span style="color:#2e9b3f; font-weight:600;">✓ 허용됨</span>';
    if (btnReq) btnReq.style.display = 'none';
  } else if (perm === 'denied') {
    if (permDesc) permDesc.innerHTML = '<span style="color:#e03131; font-weight:600;">✕ 차단됨</span> (브라우저 주소창 설정에서 해제 필요)';
    if (btnReq) {
      btnReq.style.display = 'inline-flex';
      btnReq.innerText = '재시도';
    }
  } else {
    if (permDesc) permDesc.innerHTML = '<span style="color:var(--text-2);">권한 요청 대기 중</span>';
    if (btnReq) {
      btnReq.style.display = 'inline-flex';
      btnReq.innerText = '권한 허용';
    }
  }
}

function onNotificationModeChange() {
  const sel = document.getElementById('select-notification-mode');
  if (!sel) return;
  try {
    localStorage.setItem(STORAGE_KEY_NOTIFICATION_MODE, sel.value);
  } catch (_) {}
  if (sel.value !== 'off' && 'Notification' in window && Notification.permission === 'default') {
    requestNotificationPermissionManual();
  } else {
    showToast('알림 모드가 저장되었습니다.');
  }
}

function onNotificationSoundChange() {
  const sel = document.getElementById('select-notification-sound');
  if (!sel) return;
  try {
    localStorage.setItem(STORAGE_KEY_NOTIFICATION_SOUND, sel.value);
  } catch (_) {}
  showToast('알림 효과음 설정이 저장되었습니다.');
}

// ─── PWA ───
let deferredPwaPrompt = null;
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').then((reg) => {
      if (reg) {
        // 안드로이드 PWA(standalone) 시작 시 서비스 워커 백그라운드 갱신 검사
        reg.update().catch(() => {});
        if (reg.waiting) {
          reg.waiting.postMessage({ type: 'SKIP_WAITING' });
        }
        reg.addEventListener('updatefound', () => {
          const newWorker = reg.installing;
          if (newWorker) {
            newWorker.addEventListener('statechange', () => {
              if (newWorker.state === 'installed' && navigator.serviceWorker.controller) {
                newWorker.postMessage({ type: 'SKIP_WAITING' });
              }
            });
          }
        });
      }
    }).catch(() => {});
  });

  let swRefreshing = false;
  navigator.serviceWorker.addEventListener('controllerchange', () => {
    if (!swRefreshing) {
      swRefreshing = true;
      if (typeof fetchAgyUsage === 'function') {
        fetchAgyUsage(true);
      }
    }
  });

  navigator.serviceWorker.addEventListener('message', (event) => {
    if (event.data && event.data.type === 'NAVIGATE_ROOM') {
      const targetId = event.data.roomId;
      if (targetId && typeof selectRoom === 'function') {
        selectRoom(targetId);
      }
    }
  });
}

function updatePwaUi() {
  const isStandalone = window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;
  const isIos = /iPad|iPhone|iPod/.test(navigator.userAgent) && !window.MSStream;
  const isSecure = window.isSecureContext || location.hostname === 'localhost' || location.hostname === '127.0.0.1';

  const installedEl = document.getElementById('pwa-status-installed');
  const installableEl = document.getElementById('pwa-status-installable');
  const iosEl = document.getElementById('pwa-status-ios');
  const insecureEl = document.getElementById('pwa-status-insecure');
  [installedEl, installableEl, iosEl, insecureEl].forEach(el => { if (el) el.style.display = 'none'; });

  if (isStandalone) { if (installedEl) installedEl.style.display = 'flex'; }
  else if (isIos) { if (iosEl) iosEl.style.display = 'block'; }
  else if (!isSecure) {
    if (insecureEl) insecureEl.style.display = 'block';
    if (installableEl) installableEl.style.display = 'flex';
  } else if (installableEl) {
    installableEl.style.display = 'flex';
  }
}

window.addEventListener('beforeinstallprompt', (e) => {
  e.preventDefault();
  deferredPwaPrompt = e;
  updatePwaUi();
});

window.addEventListener('appinstalled', () => {
  deferredPwaPrompt = null;
  updatePwaUi();
  showToast('앱이 설치되었습니다.');
});

async function triggerPwaInstall() {
  if (deferredPwaPrompt) {
    deferredPwaPrompt.prompt();
    await deferredPwaPrompt.userChoice;
    deferredPwaPrompt = null;
    updatePwaUi();
  } else {
    showToast('브라우저 주소창 오른쪽의 설치 아이콘이나 메뉴의 [앱 설치]를 선택해 주세요.');
  }
}

// ─── 구글 서비스(OAuth2 / Gmail / Calendar) 연동 관리 ───
let cachedGoogleStatus = null;

async function loadGoogleAuthStatus() {
  const descEl = document.getElementById('google-auth-status-desc');
  const btnConnect = document.getElementById('btn-google-auth-connect');
  const btnDisconnect = document.getElementById('btn-google-auth-disconnect');
  const rowGmail = document.getElementById('row-gmail-sync');
  const gmailDescEl = document.getElementById('gmail-sync-status-desc');

  if (!descEl) return;

  try {
    const res = await fetch('/api/google/status');
    const data = await res.json();
    if (!res.ok || data.status !== 'ok') return;

    cachedGoogleStatus = data;
    const acct = data.account || {};
    const gmail = data.gmail || {};

    if (acct.isAuthenticated) {
      const email = acct.email ? ` (${acct.email})` : '';
      descEl.innerHTML = `<span style="color:var(--accent, #4ade80); font-weight:600;">🟢 연동됨</span>${escapeHtml(email)}`;
      if (btnConnect) btnConnect.style.display = 'none';
      if (btnDisconnect) btnDisconnect.style.display = 'inline-block';
      if (rowGmail) rowGmail.style.display = 'flex';

      if (gmailDescEl) {
        if (gmail.lastSyncDisplay) {
          gmailDescEl.innerText = `최근 동기화: ${gmail.lastSyncDisplay} (메일 ${gmail.totalCount}건, 미열람 ${gmail.unreadCount}건)`;
        } else {
          gmailDescEl.innerText = '아직 동기화된 메일이 없습니다. [지금 메일 수신]을 눌러보세요.';
        }
      }
    } else {
      if (acct.hasCredentials) {
        descEl.innerHTML = `<span style="color:var(--text-3);">⚪ 미연동 (구글 계정 연동 버튼을 눌러 로그인하세요)</span>`;
      } else {
        descEl.innerHTML = `<span style="color:#f59e0b; font-weight:500;">🟡 클라이언트 설정 필요 (credentials.json 미등록)</span>`;
      }
      if (btnConnect) btnConnect.style.display = 'inline-block';
      if (btnDisconnect) btnDisconnect.style.display = 'none';
      if (rowGmail) rowGmail.style.display = 'none';
    }
  } catch (err) {
    console.error('Google 상태 로드 실패:', err);
  }
}

async function startGoogleAuth() {
  try {
    const res = await fetch('/api/google/auth/url');
    const data = await res.json();
    if (!res.ok || data.status !== 'ok') {
      showToast(data.error || '구글 인증 URL 생성에 실패했습니다.', 'error');
      // 자격증명이 없으면 고급 설정 열기
      toggleGoogleAdvancedSettings(true);
      return;
    }

    const popup = window.open(
      data.authUrl,
      'google_auth_window',
      'width=540,height=700,status=no,toolbar=no,menubar=no'
    );
    if (!popup || popup.closed || typeof popup.closed === 'undefined') {
      // 팝업 차단 시 직접 이동
      location.href = data.authUrl;
    } else {
      showToast('구글 로그인 창이 열렸습니다. 인증을 완료해주세요.');
    }
  } catch (err) {
    showToast(`인증 요청 실패: ${err.message}`, 'error');
  }
}

async function disconnectGoogleAuth() {
  if (!confirm('구글 서비스(Gmail/Calendar) 연동을 해제하시겠습니까?')) return;
  try {
    const res = await fetch('/api/google/disconnect', { method: 'POST' });
    const data = await res.json();
    if (data.ok || data.status === 'ok') {
      showToast('구글 계정 연동이 해제되었습니다.');
      loadGoogleAuthStatus();
    } else {
      showToast(data.error || '연동 해제에 실패했습니다.', 'error');
    }
  } catch (err) {
    showToast(`연동 해제 오류: ${err.message}`, 'error');
  }
}

async function syncGmailNow() {
  const btn = document.getElementById('btn-gmail-sync-now');
  if (btn) {
    btn.disabled = true;
    btn.innerText = '수신 중…';
  }
  try {
    const res = await fetch('/api/gmail/sync', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query: 'newer_than:7d', maxResults: 20 })
    });
    const data = await res.json();
    if (res.ok && data.status === 'ok') {
      const r = data.result || {};
      showToast(`📬 메일 ${r.totalCount || 0}건 수신 완료 (미열람 ${r.unreadCount || 0}건)`);
      loadGoogleAuthStatus();
    } else {
      showToast(data.error || '메일 수신에 실패했습니다.', 'error');
    }
  } catch (err) {
    showToast(`메일 동기화 오류: ${err.message}`, 'error');
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerText = '지금 메일 수신';
    }
  }
}

async function addGmailSchedulePreset() {
  try {
    const res = await fetch('/api/schedules/ensure-gmail', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ cron: '0 8 * * *' })
    });
    const data = await res.json();
    if (res.ok && data.status === 'ok') {
      showToast(data.message || 'Gmail 정기 스케줄이 등록되었습니다.');
      if (typeof window.loadAutomationData === 'function') {
        window.loadAutomationData();
      }
    } else {
      showToast(data.error || '스케줄 등록에 실패했습니다.', 'error');
    }
  } catch (err) {
    showToast(`스케줄 등록 오류: ${err.message}`, 'error');
  }
}

function toggleGoogleAdvancedSettings(forceOpen) {
  const panel = document.getElementById('google-advanced-settings');
  const btn = document.getElementById('btn-toggle-google-adv');
  if (!panel) return;
  const shouldOpen = forceOpen !== undefined ? forceOpen : panel.style.display === 'none';
  panel.style.display = shouldOpen ? 'block' : 'none';
  if (btn) btn.innerText = shouldOpen ? '설정 닫기' : '설정 열기';
}

async function submitGoogleAuthCode() {
  const input = document.getElementById('input-google-auth-code');
  const code = (input?.value || '').trim();
  if (!code) {
    showToast('인증 코드를 입력해주세요.', 'error');
    return;
  }
  try {
    const res = await fetch('/api/google/auth/code', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code })
    });
    const data = await res.json();
    if (res.ok && data.status === 'ok') {
      showToast('구글 계정이 성공적으로 연동되었습니다.');
      if (input) input.value = '';
      toggleGoogleAdvancedSettings(false);
      loadGoogleAuthStatus();
    } else {
      showToast(data.error || '인증 처리에 실패했습니다.', 'error');
    }
  } catch (err) {
    showToast(`코드 등록 오류: ${err.message}`, 'error');
  }
}

async function saveGoogleCredentialsJson() {
  const textarea = document.getElementById('textarea-google-credentials');
  const val = (textarea?.value || '').trim();
  if (!val) {
    showToast('credentials JSON을 입력해주세요.', 'error');
    return;
  }
  try {
    let parsed;
    try {
      parsed = JSON.parse(val);
    } catch (_) {
      showToast('올바른 JSON 형식이 아닙니다.', 'error');
      return;
    }
    const res = await fetch('/api/google/credentials', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ credentials: parsed })
    });
    const data = await res.json();
    if (res.ok && data.status === 'ok') {
      showToast('구글 자격증명 파일이 성공적으로 저장되었습니다.');
      if (textarea) textarea.value = '';
      loadGoogleAuthStatus();
    } else {
      showToast(data.error || '자격증명 저장 실패', 'error');
    }
  } catch (err) {
    showToast(`저장 오류: ${err.message}`, 'error');
  }
}

// 팝업 창 등에서 전달되는 성공 메시지 리스너
window.addEventListener('message', (event) => {
  if (event.data && event.data.type === 'google_auth_success') {
    showToast(`구글 연동 완료: ${event.data.email || '인증 성공'}`);
    loadGoogleAuthStatus();
  }
});
