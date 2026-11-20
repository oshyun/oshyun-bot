// ─── 상태 및 설정 ───
function applyStatusPayload(data) {
  if (!data) return;
  serverStatus = data;

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
