/* Буквоград: офлайн-кэш. Поднимайте версию при изменении файлов.
   Аудиоклипы (audio/*.m4a) кэшируются лениво при первом воспроизведении. */
const CACHE = 'bukvograd-v2';
const CORE = ['./', './index.html', './manifest.webmanifest', './icon.svg'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(CORE)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);

  // Навигация: сначала сеть (свежие обновления), офлайн — кэш
  if (req.mode === 'navigate') {
    e.respondWith(
      fetch(req).then(res => {
        if (res && res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then(c => c.put('./index.html', copy));
        }
        return res;
      }).catch(() => caches.match('./index.html').then(r => r || caches.match('./')))
    );
    return;
  }

  // Свои файлы и шрифты Google: сначала кэш, затем сеть с докэшированием
  const isFont = /fonts\.(googleapis|gstatic)\.com$/.test(url.hostname);
  if (url.origin === location.origin || isFont) {
    e.respondWith(
      caches.match(req).then(hit => hit || fetch(req).then(res => {
        // 404/ошибки не кэшируем, иначе битые ответы застревают навсегда
        if (res && (res.ok || res.type === 'opaque')) {
          const copy = res.clone();
          caches.open(CACHE).then(c => c.put(req, copy));
        }
        return res;
      }).catch(() => caches.match(req)))
    );
  }
});
