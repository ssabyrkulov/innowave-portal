// Сервис-воркер InnoWave-портала.
// Задача: сделать сайт устанавливаемым как приложение и держать иконки под
// рукой. Данные (API) НИКОГДА не кэшируются — цифры всегда свежие.
//
// Страницу (index.html) не кэшируем сознательно. Раньше кэшировали, и при
// любой заминке сети — например, в момент выкладки — браузеру отдавалась
// старая страница. Она ссылается на файл сборки с прежним именем, а его
// после выкладки уже нет: получалось белое окно, которое не лечилось
// обновлением, потому что кэш отдавался снова и снова. Пусть лучше при
// обрыве сети будет честная ошибка браузера, чем битая страница.
const VERSION = 'iw-v2'
const SHELL = [
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

  // Навигации (открытие страниц SPA) — только сеть. Страница обязана быть
  // той же версии, что и файлы сборки, которые она подтягивает.
  if (req.mode === 'navigate') return

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
