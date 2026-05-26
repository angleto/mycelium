import { useEffect, useMemo, useState } from 'react'
import { authFetch } from '../api/client'

// Process-wide refcounted cache of auth-fetched blob object URLs.
//
// Markdown images embedded as `![alt](/attachments/<id>/download)` are
// served from a bearer-authenticated endpoint, so a plain <img src=...>
// would 401. This hook fetches the bytes via authFetch, wraps them in
// an object URL, and shares the URL across every consumer that asks for
// the same path -- one network roundtrip even if the same image appears
// in the editor live preview AND in the read-side rendering at once.
// The last consumer to unmount revokes the URL.

type Entry = {
  refcount: number
  promise: Promise<string | null>
  url: string | null
}

const cache: Map<string, Entry> = new Map()

function acquire(src: string): Entry {
  const existing = cache.get(src)
  if (existing) {
    existing.refcount += 1
    return existing
  }
  const promise = (async () => {
    const res = await authFetch(src)
    if (!res.ok) return null
    const blob = await res.blob()
    return URL.createObjectURL(blob)
  })()
  const entry: Entry = { refcount: 1, promise, url: null }
  cache.set(src, entry)
  void promise.then((u) => {
    const cur = cache.get(src)
    if (cur && u) cur.url = u
  })
  return entry
}

function release(src: string): void {
  const cur = cache.get(src)
  if (!cur) return
  cur.refcount -= 1
  if (cur.refcount > 0) return
  cache.delete(src)
  // Defer revoke until the in-flight promise settles so a fast
  // mount/unmount cycle does not orphan a half-built object URL.
  void cur.promise.then((u) => {
    if (u) URL.revokeObjectURL(u)
  })
}

function isAuthPath(src: string | undefined | null): src is string {
  return !!src && src.startsWith('/attachments/')
}

/**
 * Resolve an attachment URL into a renderable image src. Returns null
 * while the auth-fetch is in flight or has failed; returns the input
 * unchanged for non-attachment URLs (http(s)://, data:, blob:).
 *
 * Pass paths relative to /api, e.g. "/attachments/<id>/download".
 */
export function useAuthBlobUrl(src: string | undefined | null): string | null {
  // Non-auth URLs (or empty) are a pure function of the input; no
  // effect, no state. Keeps the eslint rule happy by leaving setState
  // only for the genuine async-resolution branch.
  const passthrough = useMemo<string | null>(() => {
    if (!src) return null
    if (isAuthPath(src)) return null
    return src
  }, [src])

  const [blobUrl, setBlobUrl] = useState<string | null>(null)

  useEffect(() => {
    if (!isAuthPath(src)) {
      // No fetch needed; clear any stale blob from a previous src.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setBlobUrl(null)
      return
    }
    let active = true
    const entry = acquire(src)
    if (entry.url) {
      setBlobUrl(entry.url)
    } else {
      setBlobUrl(null)
      void entry.promise.then((u) => {
        if (active && u) setBlobUrl(u)
      })
    }
    return () => {
      active = false
      release(src)
    }
  }, [src])

  return passthrough ?? blobUrl
}
