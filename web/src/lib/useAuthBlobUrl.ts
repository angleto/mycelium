import { useEffect, useMemo, useState } from 'react'
import { authFetch } from '../api/client'
import type { ImageUploadParent } from './imageUpload'
import {
  ensureAttachmentManifest,
  isAttachmentManifestLoaded,
  resolveAttachmentName,
} from './attachmentManifest'

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

export type AttachmentImageState = {
  // Renderable image src (object URL, or a passed-through absolute URL),
  // or null when there is nothing to show.
  url: string | null
  // True while a fetch / manifest lookup is genuinely in flight. When
  // false and url is null the reference could not be resolved (unknown
  // filename, or the auth fetch failed) — the caller should render a
  // broken-image placeholder, NOT an indefinite spinner.
  loading: boolean
}

type SrcKind = 'empty' | 'auth' | 'absolute' | 'name'

function classifySrc(src: string | undefined | null): SrcKind {
  if (!src) return 'empty'
  if (src.startsWith('/attachments/')) return 'auth'
  // A URI scheme (http:, https:, data:, blob:, mailto:…), an absolute
  // path, or a fragment is used as-is. Everything else is treated as a
  // bare filename to resolve against the parent's attachments.
  if (/^[a-zA-Z][a-zA-Z\d+.-]*:/.test(src)) return 'absolute'
  if (src.startsWith('/') || src.startsWith('#')) return 'absolute'
  return 'name'
}

/**
 * Resolve a markdown image src into something an <img> can render,
 * reporting an explicit loading-vs-broken state.
 *
 * Three src shapes are handled:
 *  - `/attachments/<id>/download` — bearer-auth route, fetched through
 *    the refcounted blob cache (an <img src> straight at it would 401).
 *  - a bare filename (`Fig02.png`) — resolved to the parent note/task's
 *    attachment of that name, then fetched like the case above. Requires
 *    `parent`; without it (or on no match) the reference is broken.
 *  - any absolute URL / data: / blob: — passed through untouched.
 *
 * Unlike the raw object-URL hook, a failed fetch or an unknown filename
 * resolves to `{ url: null, loading: false }` so the UI stops spinning
 * and shows a broken-image placeholder instead.
 */
export function useAttachmentImage(
  src: string | undefined | null,
  parent?: ImageUploadParent,
): AttachmentImageState {
  const kind = useMemo(() => classifySrc(src), [src])

  // Bump when an async manifest load or blob fetch settles, so the
  // synchronous derivations below re-read the module caches. setState
  // happens only inside promise callbacks (never synchronously in an
  // effect), which keeps the render free of cascading-render lint.
  const [tick, setTick] = useState(0)
  const [failedPath, setFailedPath] = useState<string | null>(null)

  // Kick the manifest fetch for an unresolved filename reference; the
  // resolution itself is derived synchronously from the cache below.
  useEffect(() => {
    if (kind !== 'name' || !parent || !src) return
    if (resolveAttachmentName(parent, src)) return
    if (isAttachmentManifestLoaded(parent)) return
    let active = true
    void ensureAttachmentManifest(parent).then(() => {
      if (active) setTick((v) => v + 1)
    })
    return () => {
      active = false
    }
    // parent is depended on via its kind/id (a fresh object each render).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kind, parent?.kind, parent?.id, src])

  // Filename → auth-path resolution (re-evaluated after a manifest load).
  const nameRes = useMemo<{ url: string | null; pending: boolean }>(() => {
    if (kind !== 'name') return { url: null, pending: false }
    if (!parent || !src) return { url: null, pending: false }
    const hit = resolveAttachmentName(parent, src)
    if (hit) return { url: hit, pending: false }
    return { url: null, pending: !isAttachmentManifestLoaded(parent) }
    // tick forces a re-read of the module-level manifest cache.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kind, parent?.kind, parent?.id, src, tick])

  // The bearer-auth path to fetch through the blob cache (direct for an
  // /attachments src, or the filename resolution result).
  const fetchPath = kind === 'auth' ? src ?? null : kind === 'name' ? nameRes.url : null

  // Drive the refcounted blob fetch; the resulting object URL is read
  // back from the cache synchronously (bumped via tick on completion).
  useEffect(() => {
    if (!fetchPath) return
    let active = true
    const entry = acquire(fetchPath)
    if (!entry.url) {
      void entry.promise.then((u) => {
        if (!active) return
        if (u) setTick((v) => v + 1)
        else setFailedPath(fetchPath)
      })
    }
    return () => {
      active = false
      release(fetchPath)
    }
  }, [fetchPath])

  const blob = useMemo<string | null>(() => {
    if (!fetchPath) return null
    return cache.get(fetchPath)?.url ?? null
    // tick forces a re-read once the fetch settles.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fetchPath, tick])

  if (kind === 'empty') return { url: null, loading: false }
  if (kind === 'absolute') return { url: src ?? null, loading: false }
  if (kind === 'name' && !nameRes.url) {
    // Unknown filename once the manifest is loaded -> broken, not loading.
    return { url: null, loading: nameRes.pending }
  }
  if (blob) return { url: blob, loading: false }
  if (fetchPath && failedPath === fetchPath) return { url: null, loading: false }
  return { url: null, loading: true }
}
