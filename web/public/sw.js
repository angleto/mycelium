// Mycelium PWA service worker. Caches the SPA shell so the home screen
// icon opens to a usable surface even with no network.
// Stays out of the way for /api/* and /mcp* — those always go to the
// network so an offline backend yields a real error, not a stale UI.
//
// CACHING STRATEGY — the shape here is load-bearing, do not collapse it
// back into one rule:
//
//   navigation (index.html)  network-first, cache as fallback
//   /assets/<name>-<hash>.*  cache-first (the hash IS the version)
//   everything else static   stale-while-revalidate
//
// v5 served navigations stale-while-revalidate, i.e. cache-first. Because
// index.html is the ONE unhashed file, a cached copy pinned the whole app
// to the deploy it came from: it named /assets/index-<oldhash>.js, that
// file was in the cache too, and every subsequent reload replayed the old
// bundle. The background revalidate refreshed the cache but the user had
// already been served — and a plain reload hit the same cache-first path
// again. Only Cmd+Shift+R, which bypasses the SW entirely, escaped. Hence
// "I have to hard-reload all the time". Navigations must reach the
// network whenever there IS a network.

// Bump on every behaviour change so old SWs are replaced atomically.
const CACHE = 'mycelium-shell-v6'

// Content-hashed build output: the filename changes whenever the bytes
// change, so a cache hit can never be stale and network-first would only
// add latency.
const IMMUTABLE_PREFIX = '/assets/'

// Never intercepted: these must reflect the server, not a cache.
// /version.json is what the running app polls to notice a new deploy —
// serving it from cache would defeat the very mechanism.
const PASSTHROUGH = ['/api/', '/mcp', '/auth/', '/admin-api/', '/version.json']

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

// Only real, same-origin, success responses are cacheable. Opaque / 30x /
// 404 / 5xx must not poison the cache.
function cacheable(res) {
  return !!res && res.status === 200 && res.type === 'basic'
}

/** Network-first: the freshest answer wins; the cache is the offline
 * safety net. Used for navigations (index.html). */
async function networkFirst(req) {
  const cache = await caches.open(CACHE)
  try {
    const res = await fetch(req)
    if (cacheable(res)) cache.put(req, res.clone())
    return res
  } catch {
    // Offline (or the server is unreachable): fall back to whatever
    // shell we last saw. `ignoreSearch` because a navigation may carry
    // query params the cached entry does not.
    const hit =
      (await cache.match(req)) ||
      (await cache.match(req, { ignoreSearch: true })) ||
      (await cache.match('/index.html'))
    return hit || offlineFallback()
  }
}

/** Cache-first: for content-hashed assets, where a hit is by
 * construction the right bytes. */
async function cacheFirst(req) {
  const cache = await caches.open(CACHE)
  const hit = await cache.match(req)
  if (hit) return hit
  try {
    const res = await fetch(req)
    if (cacheable(res)) cache.put(req, res.clone())
    return res
  } catch {
    return offlineFallback()
  }
}

/** Stale-while-revalidate: instant from cache, refreshed behind the
 * user's back. Safe for unversioned auxiliaries (icons, manifest) where
 * being one visit behind costs nothing. */
async function staleWhileRevalidate(req) {
  const cache = await caches.open(CACHE)
  const hit = await cache.match(req)
  const networkPromise = fetch(req)
    .then((res) => {
      if (cacheable(res)) cache.put(req, res.clone())
      return res
    })
    .catch(() => null)
  if (hit) return hit
  const fresh = await networkPromise
  return fresh ?? offlineFallback()
}

self.addEventListener('fetch', (event) => {
  const req = event.request
  if (req.method !== 'GET') return
  const url = new URL(req.url)
  // Cross-origin (fonts, third-party) is not ours to cache or to break.
  if (url.origin !== self.location.origin) return
  if (PASSTHROUGH.some((p) => url.pathname.startsWith(p))) return

  // Critical invariant for every branch below: respondWith MUST resolve
  // to a Response, never undefined — otherwise the browser logs "Failed
  // to convert value to 'Response'" and the entire SW context throws
  // (every subsequent navigation in this tab fails). Each helper ends in
  // a Response or offlineFallback().
  if (req.mode === 'navigate') {
    event.respondWith(networkFirst(req))
    return
  }
  if (url.pathname.startsWith(IMMUTABLE_PREFIX)) {
    event.respondWith(cacheFirst(req))
    return
  }
  event.respondWith(staleWhileRevalidate(req))
})

// The page asks for a clean slate before reloading onto a new build
// (lib/useBuildWatch.ts). Dropping the shell cache guarantees the
// reload cannot be answered from anything this SW kept.
self.addEventListener('message', (event) => {
  if (event.data && event.data.type === 'MYCELIUM_DROP_CACHE') {
    event.waitUntil(
      caches.keys().then((keys) => Promise.all(keys.map((k) => caches.delete(k)))),
    )
  }
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
