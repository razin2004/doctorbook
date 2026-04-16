/* ─────────────────────────────────────────────────────────────────
   PrimeCare Service Worker  v5
   Handles: Caching, Push Notifications, Notification Click
   Key fix: All notifications visible in Android notification shade
───────────────────────────────────────────────────────────────── */

const CACHE_NAME = 'primecare-v5';
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
    return; // never cache dynamic endpoints
  }
  event.respondWith(
    caches.match(event.request).then(r => r || fetch(event.request))
  );
});

// ── Notification Click ────────────────────────────────────────────
self.addEventListener('notificationclick', event => {
  event.notification.close();
  const targetUrl = (event.notification.data && event.notification.data.url)
    ? event.notification.data.url
    : '/patient_dashboard';

  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(clientList => {
      for (const client of clientList) {
        if (client.url.includes('/patient_dashboard') || client.url === self.location.origin + '/') {
          return client.focus();
        }
      }
      return clients.openWindow(targetUrl);
    })
  );
});

// ── Notification Close ────────────────────────────────────────────
self.addEventListener('notificationclose', event => {
  // lifecycle hook — keep for future analytics
});

// ── Message from page → show notification ─────────────────────────
// Using event.waitUntil ensures Android does not kill the SW mid-notification
self.addEventListener('message', event => {
  if (!event.data) return;

  if (event.data.type === 'SHOW_NOTIFICATION') {
    const d = event.data;

    // IMPORTANT: never use silent:true for live-tracking on Android — it hides in shade
    // Use silent:false always; rely on tag+renotify to avoid sound spam
    const options = {
      body:     d.body,
      icon:     '/static/android-chrome-192x192.png',
      badge:    '/static/favicon-32x32.png',
      tag:      d.tag || 'primecare-token',
      renotify: d.renotify !== false,   // true = update shows new entry in shade
      silent:   false,                  // MUST be false — silent notifications are hidden in Android shade
      data:     { url: d.url || '/patient_dashboard' }
    };

    // Only add vibrate for alert-type notifications (not for silent live updates)
    if (d.vibrate && Array.isArray(d.vibrate)) {
      options.vibrate = d.vibrate;
    }

    event.waitUntil(
      self.registration.showNotification(d.title, options)
    );
  }

  if (event.data.type === 'SKIP_WAITING') {
    self.skipWaiting();
  }
});

// ── Web Push Event (Background delivery) ──────────────────────────
self.addEventListener('push', event => {
  if (!event.data) return;
  
  try {
    const data = event.data.json();
    const title = data.title || 'PrimeCare Update';
    const options = {
      body: data.body || '',
      icon: '/static/android-chrome-192x192.png',
      badge: '/static/favicon-32x32.png',
      tag: data.tag || 'primecare-token',
      renotify: true,
      silent: data.silent === true,
      data: { url: data.url || '/patient_dashboard' }
    };
    if (data.vibrate) options.vibrate = data.vibrate;

    event.waitUntil(
      self.registration.showNotification(title, options)
    );
  } catch (err) {
    console.error('[SW] Push event error:', err);
  }
});
