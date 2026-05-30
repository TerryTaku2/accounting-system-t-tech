const APP_CACHE = 'ttech-app-v2';
const API_CACHE = 'ttech-api-v1';
const SHELL     = ['/', '/manifest.json'];

// ── Install ────────────────────────────────────────────────────────────────────
self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(APP_CACHE)
      .then(c => c.addAll(SHELL))
      .then(() => self.skipWaiting())
  );
});

// ── Activate ───────────────────────────────────────────────────────────────────
self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(
        keys.filter(k => k !== APP_CACHE && k !== API_CACHE).map(k => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

// ── Fetch ──────────────────────────────────────────────────────────────────────
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);

  // ── API GET: network-first, API cache as fallback ─────────────────────────
  if (url.pathname.startsWith('/api/') && e.request.method === 'GET') {
    e.respondWith(
      fetch(e.request.clone())
        .then(res => {
          if (res.ok) {
            const clone = res.clone();
            caches.open(API_CACHE).then(c => c.put(e.request, clone));
          }
          return res;
        })
        .catch(async () => {
          const hit = await caches.match(e.request, { cacheName: API_CACHE });
          if (hit) return hit;
          // Empty-array fallback so list views render gracefully offline
          return new Response(JSON.stringify([]), {
            status: 200,
            headers: {
              'Content-Type': 'application/json',
              'X-Served-From': 'sw-cache',
            },
          });
        })
    );
    return;
  }

  // ── API writes: pass through; 503 when offline ────────────────────────────
  if (url.pathname.startsWith('/api/')) {
    e.respondWith(
      fetch(e.request).catch(() =>
        new Response(
          JSON.stringify({ detail: 'You are offline. Request queued.' }),
          { status: 503, headers: { 'Content-Type': 'application/json' } }
        )
      )
    );
    return;
  }

  // ── App shell: cache-first, network fallback ──────────────────────────────
  e.respondWith(
    caches.match(e.request).then(hit => {
      if (hit) return hit;
      return fetch(e.request).then(res => {
        if (res.ok && e.request.method === 'GET') {
          const clone = res.clone();
          caches.open(APP_CACHE).then(c => c.put(e.request, clone));
        }
        return res;
      }).catch(() => caches.match('/'));
    })
  );
});

// ── Background sync ────────────────────────────────────────────────────────────
self.addEventListener('sync', e => {
  if (e.tag === 'ttech-sync') {
    e.waitUntil(
      self.clients.matchAll({ includeUncontrolled: true }).then(clients =>
        clients.forEach(c => c.postMessage({ type: 'SYNC_REQUESTED' }))
      )
    );
  }
});

// ── Push notifications (Phase 4 hook) ─────────────────────────────────────────
self.addEventListener('push', e => {
  const data = e.data?.json() ?? { title: 'T-Tech Accountant', body: 'New notification' };
  e.waitUntil(
    self.registration.showNotification(data.title || 'T-Tech Accountant', {
      body:  data.body  || '',
      icon:  '/static/icon-192.png',
      badge: '/static/icon-192.png',
      data:  data,
    })
  );
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  e.waitUntil(clients.openWindow('/'));
});
