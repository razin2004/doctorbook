const CACHE_NAME = 'primecare-v1';
const ASSETS_TO_CACHE = [
  '/static/style.css',
  '/static/primecare-logo.svg',
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

// Handle Notification Click
self.addEventListener('notificationclick', event => {
  event.notification.close();
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(clientList => {
      if (clientList.length > 0) {
        let client = clientList[0];
        for (let i = 0; i < clientList.length; i++) {
          if (clientList[i].focused) {
            client = clientList[i];
          }
        }
        return client.focus();
      }
      return clients.openWindow('/patient_dashboard');
    })
  );
});

// Listen for messages from the page (to trigger background notifications)
self.addEventListener('message', event => {
  if (event.data && event.data.type === 'SHOW_NOTIFICATION') {
    self.registration.showNotification(event.data.title, {
      body: event.data.body,
      icon: event.data.icon,
      badge: event.data.icon,
      vibrate: [200, 100, 200]
    });
  }
});
