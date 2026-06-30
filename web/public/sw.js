// Mycelium PWA service worker. Cache-on-fetch for the SPA shell so the
// home screen icon opens to a usable surface even with no network.
// Stays out of the way for /api/* and /mcp* — those always go to the
// network so an offline backend yields a real error, not a stale UI.

// Bump on every behaviour change so old SWs are replaced atomically.
const CACHE = 'mycelium-shell-v5'

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

// Build a real Response for the offline fallback path so respondWith
// never receives undefined (which crashes the SW with
// "Failed to convert value to 'Response'" and kills navigation).
function offlineFallback() {
  return new Response(
    '<!doctype html><meta charset="utf-8"><title>Offline</title>' +
      '<p>You are offline and this page is not cached yet.</p>',
    { status: 503, headers: { 'Content-Type': 'text/html; charset=utf-8' } },
  )
}

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
  // the cache. Critical invariant: respondWith MUST resolve to a
  // Response, never undefined — otherwise the browser logs
  // "Failed to convert value to 'Response'" and the entire SW context
  // throws (every subsequent navigation in this tab fails).
  event.respondWith(
    (async () => {
      const cache = await caches.open(CACHE)
      const hit = await cache.match(req)
      const networkPromise = fetch(req)
        .then((res) => {
          // Only cache real, same-origin, success responses (200 +
          // basic). Opaque / 30x / 404 / 5xx must not poison the cache.
          if (res && res.status === 200 && res.type === 'basic') {
            cache.put(req, res.clone())
          }
          return res
        })
        .catch(() => null)
      if (hit) return hit
      const fresh = await networkPromise
      return fresh ?? offlineFallback()
    })(),
  )
})

// Web Push (#D): the backend sends {title, body} via the Push API. Show it
// as a system notification; userVisibleOnly subscriptions REQUIRE a visible
// notification per push or the browser drops the subscription.
self.addEventListener('push', (event) => {
  let data = {}
  try {
    data = event.data ? event.data.json() : {}
  } catch {
    data = {}
  }
  const title = data.title || 'Mycelium'
  const body = typeof data.body === 'string' ? data.body : ''
  event.waitUntil(
    self.registration.showNotification(title, {
      body,
      tag: 'mycelium-reminder',
      icon: '/icon-192.png',
      badge: '/icon-192.png',
    }),
  )
})

// Clicking the notification opens the referenced task. The reminder body
// ends with a deep-link (e.g. https://mycelium.xeno.garden/tasks/<id>); focus an
// existing Mycelium tab and navigate it there, or open a new one.
self.addEventListener('notificationclick', (event) => {
  event.notification.close()
  const match = (event.notification.body || '').match(/https?:\/\/\S+/)
  const url = match ? match[0] : '/'
  event.waitUntil(
    (async () => {
      const all = await self.clients.matchAll({ type: 'window', includeUncontrolled: true })
      for (const client of all) {
        if ('focus' in client) {
          try {
            await client.navigate(url)
          } catch {
            /* navigate can reject (cross-origin / not controlled); focus anyway */
          }
          return client.focus()
        }
      }
      if (self.clients.openWindow) return self.clients.openWindow(url)
      return undefined
    })(),
  )
})
