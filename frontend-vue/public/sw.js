const CACHE_VERSION = new URL(self.location.href).searchParams.get('v') || 'dev'
const CACHE_NAME = `music-organizer-shell-${CACHE_VERSION}`
const SHELL_ASSETS = ['/app-icon.svg', '/manifest.webmanifest']

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_ASSETS)))
  self.skipWaiting()
})

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(
      keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key)),
    )),
  )
  self.clients.claim()
})

self.addEventListener('fetch', (event) => {
  const requestUrl = new URL(event.request.url)
  if (event.request.method !== 'GET' || requestUrl.origin !== self.location.origin) return
  if (requestUrl.pathname.startsWith('/api/')) return

  if (SHELL_ASSETS.includes(requestUrl.pathname)) {
    event.respondWith(
      caches.match(event.request).then((cached) => cached || fetch(event.request)),
    )
  }
})
