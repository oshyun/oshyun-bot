// oshyun-bot PWA Service Worker
const CACHE_NAME = 'oshyun-hub-v15';
const CORE_ASSETS = [
  '/manifest.json',
  '/favicon.svg',
  '/favicon.ico',
  '/icons/favicon.svg',
  '/icons/icon-192.png',
  '/icons/icon-512.png'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      return cache.addAll(CORE_ASSETS).catch(() => {});
    }).then(() => self.skipWaiting())
  );
});

self.addEventListener('message', (event) => {
  if (event.data && event.data.type === 'SKIP_WAITING') {
    self.skipWaiting();
  }
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => {
      return Promise.all(
        keys.map((key) => {
          if (key !== CACHE_NAME) {
            return caches.delete(key);
          }
        })
      );
    }).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // WebSocket(/ws), API(/api/*), 인증(/login, /logout), 루트 HTML(/)은 캐시하지 않고 항상 네트워크 직접 전달
  if (
    url.pathname === '/' ||
    url.pathname.endsWith('.html') ||
    url.pathname.startsWith('/ws') ||
    url.pathname.startsWith('/api/') ||
    url.pathname.startsWith('/login') ||
    url.pathname.startsWith('/logout') ||
    event.request.method !== 'GET'
  ) {
    return;
  }

  // 아이콘 및 manifest 등 정적 에셋: 네트워크 우선, 실패 시 캐시 폴백
  event.respondWith(
    fetch(event.request)
      .then((response) => {
        if (response && response.status === 200 && response.type === 'basic') {
          const responseClone = response.clone();
          caches.open(CACHE_NAME).then((cache) => {
            cache.put(event.request, responseClone);
          });
        }
        return response;
      })
      .catch(() => caches.match(event.request))
  );
});

// 브라우저 및 시스템 알림 클릭 시 대화방으로 포커스/이동
self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const rawData = event.notification.data || {};
  const roomId = rawData.roomId || '';
  const targetUrl = rawData.url || (roomId ? `/?room=${encodeURIComponent(roomId)}` : '/');

  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clientList) => {
      for (const client of clientList) {
        if ('focus' in client) {
          return client.focus().then(() => {
            if (roomId) {
              client.postMessage({ type: 'NAVIGATE_ROOM', roomId });
            }
          });
        }
      }
      if (self.clients.openWindow) {
        return self.clients.openWindow(targetUrl);
      }
    })
  );
});
