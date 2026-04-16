/* ─────────────────────────────────────────────────────────────────
   PrimeCare Service Worker  v4
   Handles: Caching, Push Notifications, Notification Click
───────────────────────────────────────────────────────────────── */

const CACHE_NAME = 'primecare-v4';
const ASSETS_TO_CACHE = [
  '/static/style.css',
  '/static/primecare-logo.svg',
  '/static/android-chrome-192x192.png',
  '/static/image/pwa-icon.png'
];

// ── Install ──────────────────────────────────────────────────────
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => cache.addAll(ASSETS_TO_CACHE))
  );
  self.skipWaiting();
});

// ── Activate ─────────────────────────────────────────────────────
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// ── Fetch (Network first for API, Cache first for statics) ───────
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (url.pathname.startsWith('/api/') ||
      url.pathname.includes('/my_token_status') ||
      url.pathname.includes('/live_tokens')) {
    return; // no cache for dynamic endpoints
  }
  event.respondWith(
    caches.match(event.request).then(r => r || fetch(event.request))
  );
});

// ── Notification Click ────────────────────────────────────────────
self.addEventListener('notificationclick', event => {
  event.notification.close();
  const targetUrl = event.notification.data?.url || '/patient_dashboard';
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(clientList => {
      for (const client of clientList) {
        if (client.url.includes('/patient_dashboard') || client.url.includes('/')) {
          return client.focus();
        }
      }
      return clients.openWindow(targetUrl);
    })
  );
});

// ── Notification Close (optional analytics/cleanup) ───────────────
self.addEventListener('notificationclose', event => {
  // Nothing needed for now, but keeps lifecycle clean
});

// ── Message from page → show notification ─────────────────────────
self.addEventListener('message', event => {
  if (!event.data) return;

  if (event.data.type === 'SHOW_NOTIFICATION') {
    const options = {
      body: event.data.body,
      icon: '/static/android-chrome-192x192.png',
      badge: '/static/favicon-32x32.png',
      tag: event.data.tag || 'primecare-token',
      renotify: event.data.renotify !== false,
      silent: event.data.silent === true,
      data: { url: event.data.url || '/patient_dashboard' }
    };
    if (event.data.vibrate) options.vibrate = event.data.vibrate;

    event.waitUntil(
      self.registration.showNotification(event.data.title, options)
    );
  }

  if (event.data.type === 'SKIP_WAITING') {
    self.skipWaiting();
  }
});
