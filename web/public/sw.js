// Flow PWA service worker. Cache-on-fetch for the SPA shell so the
// home screen icon opens to a usable surface even with no network.
// Stays out of the way for /api/* and /mcp* — those always go to the
// network so an offline backend yields a real error, not a stale UI.

const CACHE = 'flow-shell-v1'

self.addEventListener('install', () => {
  // The first activation is fine without any preload — the SPA bundle
  // will be cached on first visit via the fetch handler below.
  self.skipWaiting()
})

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)),
      ),
    ),
  )
  self.clients.claim()
})

self.addEventListener('fetch', (event) => {
  const req = event.request
  if (req.method !== 'GET') return
  const url = new URL(req.url)
  // Never intercept API / MCP / auth — these must always touch the
  // network. The SW serves only the bundled SPA assets + index.html.
  if (
    url.pathname.startsWith('/api/') ||
    url.pathname.startsWith('/mcp') ||
    url.pathname.startsWith('/auth/') ||
    url.pathname.startsWith('/admin-api/')
  ) {
    return
  }
  // Stale-while-revalidate for the SPA shell: serve cached if present,
  // refresh in the background. New tab on a fresh install bootstraps
  // the cache.
  event.respondWith(
    caches.open(CACHE).then(async (cache) => {
      const hit = await cache.match(req)
      const fetched = fetch(req)
        .then((res) => {
          if (res && res.status === 200 && res.type === 'basic') {
            cache.put(req, res.clone())
          }
          return res
        })
        .catch(() => hit) // offline: best-effort serve cached
      return hit || fetched
    }),
  )
})
