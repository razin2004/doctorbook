const CACHE_NAME = 'primecare-v3';
const ASSETS_TO_CACHE = [
  '/static/style.css',
  '/static/primecare-logo.svg',
  '/static/android-chrome-192x192.png',
  '/static/image/pwa-icon.png'
];

// Install Event
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => {
      return cache.addAll(ASSETS_TO_CACHE);
    })
  );
  self.skipWaiting();
});

// Activate Event
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys => {
      return Promise.all(keys.filter(key => key !== CACHE_NAME).map(key => caches.delete(key)));
    })
  );
  self.clients.claim();
});

// Fetch Event (Network First for API, Cache First for Assets)
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  
  // Don't cache API calls
  if (url.pathname.includes('/api/') || url.pathname.includes('/my_token_status')) {
    return;
  }

  event.respondWith(
    caches.match(event.request).then(response => {
      return response || fetch(event.request);
    })
  );
});

// Handle Notification Click — open/focus the app
self.addEventListener('notificationclick', event => {
  event.notification.close();
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(clientList => {
      for (const client of clientList) {
        if (client.url.includes('/patient_dashboard') || client.url.includes('/') || client.url.includes('/live-tracking')) {
          return client.focus();
        }
      }
      return clients.openWindow('/patient_dashboard');
    })
  );
});

// Listen for messages from the page — unified notification sender
self.addEventListener('message', event => {
  if (!event.data) return;

  const { type, title, body, tag, vibrate, silent, renotify } = event.data;

  if (type === 'SHOW_NOTIFICATION') {
    const options = {
      body: body || '',
      icon: '/static/android-chrome-192x192.png',
      badge: '/static/favicon-32x32.png',
      tag: tag || 'primecare-general',
      renotify: renotify !== undefined ? renotify : true,
      silent: silent !== undefined ? silent : false,
      requireInteraction: false,
    };

    if (vibrate && vibrate.length) {
      options.vibrate = vibrate;
    }

    self.registration.showNotification(title || 'PrimeCare', options);
  }
});
