// Shared cache + fetch for the ``GET /lookup/{prefix}`` endpoint.
//
// Roadmap notes refer to tasks / notes by an 8-char UUID prefix in
// backticks (`91cf6aaa`). The markdown renderer turns those into
// clickable chips, and the short-URL routes /n/:prefix and /t/:prefix
// hit the same cache. Module-level so the prefix that occurs N times
// in the same document collapses to one round trip.
//
// We intentionally do *not* coalesce concurrent same-page lookups via
// React state: the renderer mounts components synchronously, so
// concurrent calls would all see ``inflight`` and await the same
// promise without spurious network traffic.
//
// The cache is keyed by ``${prefix}|${kindsKey}`` because the resolver
// can be invoked with a different kinds whitelist. We don't expire
// entries in-process: archived/deleted upgrades or task title edits
// are eventually consistent for chip rendering, which is acceptable
// (the user can refresh to re-read). The cache is wiped on logout via
// the SPA's existing token-change reset path (same module reload model
// as ``taskMentionCache``).

import { authFetch } from '../api/client'

export interface LookupMatch {
  kind: 'task' | 'note'
  id: string
  title: string | null
  state_name: string | null
  is_terminal: boolean | null
  is_archived: boolean
  is_deleted: boolean
  route_url: string
}

export interface LookupOut {
  prefix: string
  matches: LookupMatch[]
}

const HEX_PREFIX_RE = /^[0-9a-f][0-9a-f-]{2,34}[0-9a-f]$/i

export function isPrefixCandidate(raw: string): boolean {
  const s = raw.trim().toLowerCase()
  return HEX_PREFIX_RE.test(s)
}

export function isFullUuid(raw: string): boolean {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-fA-F]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(raw.trim())
}

const cache = new Map<string, LookupOut>()
const inflight = new Map<string, Promise<LookupOut | null>>()

function cacheKey(prefix: string, kinds: readonly ('task' | 'note')[] | undefined): string {
  const k = kinds && kinds.length ? [...kinds].sort().join(',') : 'task,note'
  return `${prefix.trim().toLowerCase()}|${k}`
}

export function getCachedLookup(
  prefix: string,
  kinds?: readonly ('task' | 'note')[],
): LookupOut | undefined {
  return cache.get(cacheKey(prefix, kinds))
}

export async function lookupPrefix(
  prefix: string,
  opts: { kinds?: readonly ('task' | 'note')[]; signal?: AbortSignal } = {},
): Promise<LookupOut | null> {
  const key = cacheKey(prefix, opts.kinds)
  const hit = cache.get(key)
  if (hit) return hit
  const pending = inflight.get(key)
  if (pending) return pending
  const p = (async () => {
    const qs = new URLSearchParams()
    if (opts.kinds && opts.kinds.length) qs.set('kinds', [...opts.kinds].join(','))
    const url = `/lookup/${encodeURIComponent(prefix.trim().toLowerCase())}${qs.toString() ? `?${qs}` : ''}`
    const res = await authFetch(url, { signal: opts.signal })
    if (!res.ok) return null
    const body = (await res.json()) as LookupOut
    cache.set(key, body)
    return body
  })()
  inflight.set(key, p)
  try {
    return await p
  } finally {
    inflight.delete(key)
  }
}

// Test seam: lets unit tests inject a deterministic response without
// hitting the network. Not exported to the prod barrel.
export function _seedCacheForTest(prefix: string, payload: LookupOut): void {
  cache.set(cacheKey(prefix, undefined), payload)
}

export function _clearCacheForTest(): void {
  cache.clear()
  inflight.clear()
}
