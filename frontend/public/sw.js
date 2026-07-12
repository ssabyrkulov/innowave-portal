// Сервис-воркер InnoWave-портала.
// Задача: сделать сайт устанавливаемым как приложение и держать оболочку
// в кэше для мгновенного запуска. Данные (API) НИКОГДА не кэшируются —
// цифры всегда свежие. Меняйте VERSION, чтобы сбросить старый кэш.
const VERSION = 'iw-v1'
const SHELL = [
  '/',
  '/manifest.webmanifest',
  '/icon-192.png',
  '/icon-512.png',
  '/apple-touch-icon.png',
]

self.addEventListener('install', (e) => {
  self.skipWaiting()
  e.waitUntil(
    caches.open(VERSION).then((c) => c.addAll(SHELL).catch(() => {}))
  )
})

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k)))
      )
      .then(() => self.clients.claim())
  )
})

self.addEventListener('fetch', (e) => {
  const req = e.request
  if (req.method !== 'GET') return
  const url = new URL(req.url)
  if (url.origin !== self.location.origin) return

  // Навигации (открытие страниц SPA): сеть первой, оффлайн — из кэша оболочки.
  if (req.mode === 'navigate') {
    e.respondWith(
      fetch(req)
        .then((res) => {
          const copy = res.clone()
          caches.open(VERSION).then((c) => c.put('/', copy)).catch(() => {})
          return res
        })
        .catch(() => caches.match('/') || caches.match(req))
    )
    return
  }

  // Собранные ассеты Vite (хешированные имена) и иконки — кэш первым.
  if (url.pathname.startsWith('/assets/') || SHELL.includes(url.pathname)) {
    e.respondWith(
      caches.match(req).then(
        (hit) =>
          hit ||
          fetch(req).then((res) => {
            const copy = res.clone()
            caches.open(VERSION).then((c) => c.put(req, copy)).catch(() => {})
            return res
          })
      )
    )
    return
  }

  // Остальное (в т.ч. API) — только сеть, без кэширования.
})
