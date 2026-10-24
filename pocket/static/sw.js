// Pocket Bus — offline SHELL only. Mail never lives on the phone.
// The page comes from cache when the bus host is unreachable, so it can say
// so ("Can't reach the bus … last good contact …") instead of going blank.
// /api/* is never cached: every letter comes from the bus, every time.
const CACHE = 'pocket-bus-shell-v1';
const SHELL = ['/app/', '/app/app.js', '/app/app.css', '/app/manifest.webmanifest',
               '/app/icon.svg', '/app/icon-192.png', '/app/icon-512.png'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== self.location.origin || !url.pathname.startsWith('/app/')) return;
  // Network first, so a new version lands right away; cache only when offline.
  e.respondWith(fetch(e.request).then((r) => {
    if (r.ok) { const copy = r.clone(); caches.open(CACHE).then((c) => c.put(e.request, copy)); }
    return r;
  }).catch(() => caches.match(e.request).then((hit) => hit || caches.match('/app/'))));
});
