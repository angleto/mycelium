import { useEffect, useMemo, useState } from 'react'
import { authFetch } from '../api/client'
import { getSession } from '../auth/session'
import type { ImageUploadParent } from './imageUpload'
import {
  attachmentBasename,
  classifyAttachmentRef,
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
//
// A failure is cached as the final answer only when it IS final. A
// transient one is retried inside the shared chain (N consumers of one src
// still cost one chain), every consumer subscribed to the entry is woken
// each time a chain settles, and an entry whose retry budget ran out is
// re-armed by the next mount instead of pinning a broken placeholder for
// the life of the page. A definitive verdict (404 gone, 403 not yours) is
// kept, so a deleted attachment is not re-requested on every remount.

// Why the entry's current load chain stopped; null while one is running and
// after one succeeded.
//  - 'permanent': the server answered about the attachment itself (404,
//    403, and every other non-retryable status). Neither another attempt
//    nor a later mount changes that answer, so acquire() leaves it alone.
//  - 'transient': the retry budget ran out on failures that may well
//    succeed later (5xx / 408 / 429 / network), or the chain stopped early
//    because there is no usable session to fetch with. A later mount
//    re-arms the entry.
type Failure = 'transient' | 'permanent'

// Notified with the chain that settled, every time one settles (subscribe).
type Waiter = (chain: Promise<string | null>) => void

type Entry = {
  // The auth path this entry caches. Kept on the entry so release() can
  // check the map still points at THIS entry before dropping the slot.
  src: string
  refcount: number
  // The current load chain. Replaced when acquire() re-arms a transiently
  // failed entry; consumers compare it against the chain they saw settle.
  promise: Promise<string | null>
  url: string | null
  // The blob's Content-Type, captured at fetch time. Authoritative for
  // media-kind dispatch (useAttachmentMedia) — null until the fetch
  // settles, and for an image-only consumer it is simply ignored.
  mime: string | null
  // Set when the current chain gave up, cleared when a re-arm starts a new
  // one. Read back during render (together with the chain identity) to tell
  // "broken" from "still trying".
  failure: Failure | null
  // The mounted consumers of this entry: registered by subscribe(), dropped
  // by its unsubscribe on unmount, so the set never outlives the components.
  // Waiters persist ACROSS chains — a consumer that already saw one chain
  // give up is woken again when a re-armed chain settles, which is what
  // stops a second embed of the same attachment from leaving the first one
  // pinned on a broken placeholder.
  waiters: Set<Waiter>
}

const cache: Map<string, Entry> = new Map()

// Bounded retry of TRANSIENT download failures. This is resilience
// hardening, NOT the fix for the 2026-07-24 outage: that one was an
// infrastructure change (the managed Postgres instance lost its
// 0.0.0.0/0 ACL rule at 16:18:31Z, after which every NEW backend
// connection was reset), it lasted far longer than any client-side budget,
// and no amount of retrying here would have recovered it.
//
// What retries do buy is the short, self-clearing failure — a rolling
// restart, one dropped connection — where a single 500 used to be cached
// process-wide as a null result, so every consumer of that src, later
// mounts included, showed a broken placeholder for the life of the page.
// That client-side amplification, not the outage itself, is what this file
// is responsible for.
//
// Budget: 3 attempts, each capped at ATTEMPT_TIMEOUT_MS, with a jittered
// backoff (under 1.2s in total) between them. A src that never answers is
// therefore broken after ~16s of waiting for responses, instead of
// spinning for as long as the backend keeps a request open. (Only the wait
// for a response is capped; see ATTEMPT_TIMEOUT_MS.)
const MAX_ATTEMPTS = 3
const RETRY_BASE_MS = 250

// Per-attempt cap on the wait for a RESPONSE. A degraded backend can hold a
// request open for tens of seconds, and an <img> placeholder that spins for
// a minute is worse than one that resolves to broken; 5s is far above a
// healthy attachment GET.
//
// The cap covers the request only: the timer is cleared as soon as the
// response headers arrive, so a large attachment on a slow link is never
// aborted mid-transfer. It bounds "this request is stuck", not "this
// download is slow".
const ATTEMPT_TIMEOUT_MS = 5_000

// Which statuses deserve another attempt: 5xx (the backend failing for a
// while), plus 408 and 429, the canonical "ask again later" codes.
// Everything else is a verdict on THIS request that a retry cannot change.
function isTransientStatus(status: number): boolean {
  return status >= 500 || status === 408 || status === 429
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

// Exponential backoff with full-width jitter. All the embeds of a note
// start together, so their retries would fire together too; jitter spreads
// the second wave instead of aiming N simultaneous requests at a backend
// that is already answering badly.
function backoffMs(attempt: number): number {
  return Math.round(RETRY_BASE_MS * 2 ** (attempt - 1) * (0.5 + Math.random()))
}

// What one attempt concluded:
//  - 'ok'        the bytes are published on the entry, the chain is done;
//  - 'retry'     transient and worth attempting again right now;
//  - 'transient' cannot succeed right now but a later mount may (no usable
//                session): stop the chain without burning the budget;
//  - 'permanent' the server's verdict on this attachment: stop for good.
type Attempt = 'ok' | 'retry' | Failure

async function runAttempt(entry: Entry): Promise<Attempt> {
  const ctl = new AbortController()
  const timer = setTimeout(() => ctl.abort(), ATTEMPT_TIMEOUT_MS)
  try {
    const res = await authFetch(entry.src, { signal: ctl.signal })
    // Headers are in, so the request is not stuck: stop the clock before
    // reading the body: that read is deliberately left uncapped (see
    // ATTEMPT_TIMEOUT_MS), and an armed signal would abort it too.
    clearTimeout(timer)
    if (res.ok) {
      const blob = await res.blob()
      // Publish onto the entry BEFORE the chain resolves so a consumer
      // woken by it reads a fully populated entry, mime included.
      entry.mime = blob.type || null
      entry.url = URL.createObjectURL(blob)
      return 'ok'
    }
    if (isTransientStatus(res.status)) return 'retry'
    // 401 means authFetch already ran its own refresh-and-retry and dropped
    // the session, so asking again now is pointless — but a mount after the
    // user signs back in is not, hence transient rather than permanent.
    return res.status === 401 ? 'transient' : 'permanent'
  } catch {
    clearTimeout(timer)
    // What lands here: the abort above, and a genuine network failure
    // (offline, TLS reset, connection dropped mid-body) — both worth
    // another attempt — but also authFetch's session guard, which throws
    // when there is no session at all (api/client.ts). That one is not
    // transient within the chain: every further attempt would throw
    // identically, so stop instead of spending the whole budget on it, and
    // stay re-armable for a mount after the user signs back in.
    return getSession() ? 'retry' : 'transient'
  }
}

// The single-flight load chain for one entry: attempt, and on a transient
// failure attempt again after a jittered backoff. Every failure is caught
// in runAttempt, so this resolves to null when it gives up rather than
// rejecting: waiters attach a bare .then (the deferred revoke in release
// included), and a rejection would both strand the consumer on an eternal
// spinner and surface as an unhandled rejection.
async function load(entry: Entry): Promise<string | null> {
  for (let attempt = 1; ; attempt += 1) {
    const outcome = await runAttempt(entry)
    if (outcome === 'ok') return entry.url
    if (outcome !== 'retry') {
      entry.failure = outcome
      return null
    }
    // Stop when the budget is spent, or when nobody is waiting any more
    // (every consumer unmounted mid-chain — the entry is already out of the
    // cache, so a later mount starts a fresh one).
    if (attempt >= MAX_ATTEMPTS || entry.refcount <= 0) {
      entry.failure = 'transient'
      return null
    }
    await sleep(backoffMs(attempt))
  }
}

// Start the entry's load chain and wake every waiter once it settles. The
// chain is published on the entry here (rather than by the caller) so it is
// in place before it can possibly settle, and it is handed to the waiters
// so a consumer can tell "the entry's verdict is the one I waited for" from
// "somebody re-armed it since I looked". Referencing `chain` from its own
// callback is safe: that callback runs in a later microtask.
function startChain(entry: Entry): void {
  const chain: Promise<string | null> = load(entry).then((url) => {
    for (const wake of Array.from(entry.waiters)) wake(chain)
    return url
  })
  entry.promise = chain
}

function acquire(src: string): Entry {
  const existing = cache.get(src)
  if (existing) {
    existing.refcount += 1
    // A previous chain spent its TRANSIENT budget. Re-arm the SAME entry
    // instead of handing out its null promise: this mount gets a fresh
    // chain, and so does every consumer already subscribed (they are woken
    // when it settles), while the entry identity — and therefore the
    // refcount held by any consumer still showing the broken placeholder,
    // and the revoke discipline that hangs off it — is preserved. A chain
    // still in flight has failure === null, so concurrent mounts stay
    // single-flight. A 'permanent' failure is deliberately NOT re-armed:
    // an attachment that is gone or forbidden would otherwise be requested
    // again on every single remount.
    if (existing.failure === 'transient') {
      existing.failure = null
      startChain(existing)
    }
    return existing
  }
  const entry: Entry = {
    src,
    refcount: 1,
    // Replaced synchronously by startChain() below: load() publishes
    // url/mime onto the entry, so the entry object must exist first, and
    // nothing reads entry.promise in between.
    promise: Promise.resolve(null),
    url: null,
    mime: null,
    failure: null,
    waiters: new Set(),
  }
  startChain(entry)
  cache.set(src, entry)
  return entry
}

// Subscribe a consumer to this entry's load outcomes for as long as it is
// mounted; returns the unsubscribe to call on cleanup. The waiter is handed
// the chain that settled, EVERY time one settles — chains started by
// another consumer's re-arm included. That is what a one-shot `.then` on
// entry.promise got wrong: a consumer told once that the chain gave up was
// never told about the later chain that succeeded, and stayed broken while
// a sibling embed of the same attachment showed the image.
function subscribe(entry: Entry, wake: Waiter): () => void {
  entry.waiters.add(wake)
  // Also cover the chain that is current right now. If it has ALREADY
  // settled, nothing else would wake this consumer (a permanent failure is
  // never re-armed) and the placeholder would spin for ever; this hands the
  // standing verdict over on the next microtask. While it is still running
  // the waiter set above already covers it and this delivers the same chain
  // twice, which consumers read as an unchanged value.
  const chain = entry.promise
  void chain.then(() => {
    if (entry.waiters.has(wake)) wake(chain)
  })
  return () => {
    entry.waiters.delete(wake)
  }
}

function release(entry: Entry): void {
  entry.refcount -= 1
  if (entry.refcount > 0) return
  // Only drop the map slot if it still points at this very entry, so a
  // late release can never evict somebody else's live entry.
  if (cache.get(entry.src) === entry) cache.delete(entry.src)
  // Defer revoke until the in-flight promise settles so a fast
  // mount/unmount cycle does not orphan a half-built object URL.
  void entry.promise.then((u) => {
    if (u) URL.revokeObjectURL(u)
  })
}

function isAuthPath(src: string | undefined | null): src is string {
  return !!src && src.startsWith('/attachments/')
}

/**
 * Resolve an attachment URL into a renderable image src. Returns null for
 * as long as there is no object URL — while the auth-fetch and its bounded
 * transient retries are in flight, and after they failed. This hook has no
 * broken state: if a later mount re-arms the src and that attempt succeeds,
 * this consumer is woken and starts rendering it too. Returns the input
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
    const entry = acquire(src)
    let unsubscribe: (() => void) | null = null
    if (entry.url) {
      setBlobUrl(entry.url)
    } else {
      setBlobUrl(null)
      // subscribe (not a bare .then on the promise) so a chain re-armed by
      // a later mount reaches this consumer too: this hook has no broken
      // state, it simply shows the media once some attempt succeeds.
      unsubscribe = subscribe(entry, () => setBlobUrl(entry.url))
    }
    return () => {
      unsubscribe?.()
      release(entry)
    }
  }, [src])

  return passthrough ?? blobUrl
}

export type AttachmentImageState = {
  // Renderable image src (object URL, or a passed-through absolute URL),
  // or null when there is nothing to show.
  url: string | null
  // True while a fetch / manifest lookup is genuinely in flight, retries
  // of a transient failure included (so the placeholder never flickers
  // broken between two attempts). When false and url is null the
  // reference could not be resolved (unknown filename, or a fetch chain
  // that gave up: for good on a 404/403, or with its retry budget spent) —
  // the caller should render a broken-image placeholder, NOT an indefinite
  // spinner.
  loading: boolean
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
 * resolves to `{ url: null, loading: false }` so the UI stops spinning and
 * shows a broken-image placeholder instead. A transient failure (5xx /
 * network) is retried under the hood and keeps reporting `loading`
 * meanwhile; a src whose retry budget ran out is retried from scratch on
 * the next mount, and whichever attempt finally succeeds un-breaks every
 * consumer of that src, not just the one that triggered it. Only a
 * definitive answer (404 / 403) is remembered as broken.
 */
export function useAttachmentImage(
  src: string | undefined | null,
  parent?: ImageUploadParent,
): AttachmentImageState {
  const kind = useMemo(() => classifyAttachmentRef(src), [src])

  // Bump when an async manifest load settles, so the synchronous
  // derivations below re-read the module caches. setState happens only
  // inside promise callbacks (never synchronously in an effect), which
  // keeps the render free of cascading-render lint.
  const [tick, setTick] = useState(0)
  // The last load chain THIS consumer saw settle. Identifying the chain
  // (not just the path) is what keeps the state machine honest across a
  // retry: a failed entry that a later mount re-armed carries a different
  // promise, and must read as 'loading' again, not as broken.
  const [settled, setSettled] = useState<Promise<string | null> | null>(null)

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
  // back from the cache synchronously (woken by `settled` on completion).
  useEffect(() => {
    if (!fetchPath) return
    const entry = acquire(fetchPath)
    // Subscribe UNCONDITIONALLY, including when entry.url already holds the
    // bytes. Skipping on a warm entry looks safe and is not: this render
    // derives everything from `settled`, and a consumer that rendered while
    // the entry was still empty, then ran its effect after the chain had
    // succeeded (React runs passive effects after paint, which is ample time
    // for a fetch already in flight to resolve), would subscribe to nothing,
    // never set state, and spin forever while a sibling embed of the same
    // attachment shows the image. subscribe() hands over the standing chain's
    // outcome for exactly this case.
    const unsubscribe = subscribe(entry, (c) => setSettled(c))
    return () => {
      unsubscribe()
      release(entry)
    }
  }, [fetchPath])

  const blob = useMemo<{ url: string | null; failed: boolean }>(() => {
    const entry = fetchPath ? cache.get(fetchPath) : undefined
    if (!entry) return { url: null, failed: false }
    // A verdict counts only for the consumers that watched the chain which
    // produced it: a fresh mount (settled === null) reads as loading until
    // a chain it subscribed to settles, and a verdict left behind by a
    // chain that has since been re-armed is not reported again.
    return { url: entry.url, failed: entry.failure !== null && entry.promise === settled }
    // The module cache is read, not depended on: `settled` (set whenever a
    // chain of ours settles) is the signal that its contents changed.
  }, [fetchPath, settled])

  if (kind === 'empty') return { url: null, loading: false }
  if (kind === 'absolute') return { url: src ?? null, loading: false }
  if (kind === 'name' && !nameRes.url) {
    // Unknown filename once the manifest is loaded -> broken, not loading.
    return { url: null, loading: nameRes.pending }
  }
  if (blob.url) return { url: blob.url, loading: false }
  // Broken only once a chain this consumer watched gave up; while one is
  // still backing off — or has just been re-armed by another mount — we
  // stay 'loading', so the UI never flashes broken between two attempts.
  if (blob.failed) return { url: null, loading: false }
  return { url: null, loading: true }
}

export type AttachmentMediaState = {
  // Object URL for an /attachments src (or a passed-through absolute URL),
  // or null while resolving / when the reference is broken.
  url: string | null
  // The blob's Content-Type once fetched (null until then, or for an
  // absolute passthrough). Authoritative input to attachmentKind().
  mime: string | null
  // Best-effort filename for the reference: the basename of a `name` ref
  // (e.g. `recording.mp3`), else null. Lets the caller refine the kind by
  // extension and label the embed. (An /attachments/<id> ref carries no
  // name; the caller can fall back to the markdown alt text.)
  name: string | null
  // True while a fetch / manifest lookup is genuinely in flight, retries
  // of a transient failure included. When false with url null the
  // reference is broken: render a placeholder, not an endless spinner.
  loading: boolean
}

/**
 * Generalised sibling of useAttachmentImage: resolves the SAME three ref
 * shapes (auth path, bare filename, absolute URL) into a renderable object
 * URL, but additionally surfaces the blob's mime and the reference's
 * filename so the caller can dispatch on attachmentKind() — image vs audio
 * vs video vs text. Shares the one refcounted blob cache, so an image
 * embedded both here and via useAttachmentImage is still fetched once.
 *
 * Kept as a separate hook (rather than refactoring useAttachmentImage to
 * delegate) so the existing image path — used by the live editor preview —
 * stays byte-identical and cannot regress.
 */
export function useAttachmentMedia(
  src: string | undefined | null,
  parent?: ImageUploadParent,
): AttachmentMediaState {
  const kind = useMemo(() => classifyAttachmentRef(src), [src])
  const name = useMemo<string | null>(
    () => (kind === 'name' && src ? attachmentBasename(src) : null),
    [kind, src],
  )

  const [tick, setTick] = useState(0)
  // See useAttachmentImage: the chain we saw settle, not just the path, is
  // what distinguishes "gave up" from "a later mount is retrying it".
  const [settled, setSettled] = useState<Promise<string | null> | null>(null)

  // Kick the manifest fetch for an unresolved filename reference.
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
    // parent depended on via kind/id (a fresh object each render).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kind, parent?.kind, parent?.id, src])

  const nameRes = useMemo<{ url: string | null; pending: boolean }>(() => {
    if (kind !== 'name') return { url: null, pending: false }
    if (!parent || !src) return { url: null, pending: false }
    const hit = resolveAttachmentName(parent, src)
    if (hit) return { url: hit, pending: false }
    return { url: null, pending: !isAttachmentManifestLoaded(parent) }
    // tick forces a re-read of the module-level manifest cache.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kind, parent?.kind, parent?.id, src, tick])

  const fetchPath =
    kind === 'auth' ? src ?? null : kind === 'name' ? nameRes.url : null

  useEffect(() => {
    if (!fetchPath) return
    const entry = acquire(fetchPath)
    // Subscribe to every outcome, warm entry included (see
    // useAttachmentImage for why the `entry.url ? null :` shortcut is a
    // permanent spinner): a give-up is what flips this consumer from
    // 'loading' to 'broken', and a later mount's successful retry is what
    // flips it back.
    const unsubscribe = subscribe(entry, (c) => setSettled(c))
    return () => {
      unsubscribe()
      release(entry)
    }
  }, [fetchPath])

  const resolved = useMemo<{
    url: string | null
    mime: string | null
    failed: boolean
  }>(() => {
    const entry = fetchPath ? cache.get(fetchPath) : undefined
    if (!entry) return { url: null, mime: null, failed: false }
    return {
      url: entry.url,
      mime: entry.mime,
      // Broken only while the chain we saw settle is still the entry's
      // current one; see useAttachmentImage.
      failed: entry.failure !== null && entry.promise === settled,
    }
    // The module cache is read, not depended on: `settled` (set whenever a
    // chain of ours settles) is the signal that its contents changed.
  }, [fetchPath, settled])

  if (kind === 'empty') return { url: null, mime: null, name, loading: false }
  if (kind === 'absolute') {
    return { url: src ?? null, mime: null, name, loading: false }
  }
  if (kind === 'name' && !nameRes.url) {
    return { url: null, mime: null, name, loading: nameRes.pending }
  }
  if (resolved.url) {
    return { url: resolved.url, mime: resolved.mime, name, loading: false }
  }
  // Broken only once a chain this consumer watched gave up (see
  // useAttachmentImage).
  if (resolved.failed) {
    return { url: null, mime: null, name, loading: false }
  }
  return { url: null, mime: null, name, loading: true }
}
