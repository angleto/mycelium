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
// The cache is keyed by ``${prefix}|${kindsKey}|${archivedKey}`` because
// the resolver can be invoked with a different kinds whitelist AND with a
// different perimeter. We don't expire entries in-process: archived /
// deleted upgrades or task title edits are eventually consistent for chip
// rendering, which is acceptable (the user can refresh to re-read). The
// cache is dropped whenever the ACTIVE WORKSPACE changes (including on
// logout), via ``clearOnWorkspaceChange``: a prefix is only unique inside
// one tenant, so an entry resolved in workspace A would otherwise route a
// chip in workspace B to an entity that is not there.
//
// TWO INTENTS, one endpoint (task d12f6217). Resolving an id -- a chip, a
// short URL, the palette's code branch -- asks "what entity is this?", and
// the answer must not depend on whether the entity sits on the archive
// shelf: those callers pass ``includeArchived`` and render the state the
// match reports. Offering a LIST of candidates (the mention picker) asks
// "what may I link to from here?", and keeps the endpoint's default, which
// is the same perimeter ``GET /notes`` and ``GET /tasks`` show. The flag is
// therefore never a detail of the fetch: it is which of the two questions
// the caller is asking.

import { authFetch } from '../api/client'
import { clearOnWorkspaceChange } from './tenantCache'

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

clearOnWorkspaceChange(() => {
  cache.clear()
  inflight.clear()
})

export interface LookupOpts {
  kinds?: readonly ('task' | 'note')[]
  /** Resolve entities on the archive shelf too. They come back with
   *  ``is_archived: true``, so the caller can (and should) show it. */
  includeArchived?: boolean
}

/** The perimeter for "what entity is this id?": the archive shelf must not
 *  hide an entity from its own identifier. Spread it (``{...RESOLVE_ID,
 *  kinds: [...]}``) so every resolution call site says which question it is
 *  asking instead of leaning on a default. */
export const RESOLVE_ID: LookupOpts = { includeArchived: true }

function cacheKey(prefix: string, opts: LookupOpts): string {
  const kinds = opts.kinds
  const k = kinds && kinds.length ? [...kinds].sort().join(',') : 'task,note'
  return `${prefix.trim().toLowerCase()}|${k}|${opts.includeArchived ? 'a' : ''}`
}

export function getCachedLookup(prefix: string, opts: LookupOpts = {}): LookupOut | undefined {
  return cache.get(cacheKey(prefix, opts))
}

export async function lookupPrefix(
  prefix: string,
  opts: LookupOpts & { signal?: AbortSignal } = {},
): Promise<LookupOut | null> {
  const key = cacheKey(prefix, opts)
  const hit = cache.get(key)
  if (hit) return hit
  const pending = inflight.get(key)
  if (pending) return pending
  const p = (async () => {
    const qs = new URLSearchParams()
    if (opts.kinds && opts.kinds.length) qs.set('kinds', [...opts.kinds].join(','))
    if (opts.includeArchived) qs.set('include_archived', 'true')
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
export function _seedCacheForTest(
  prefix: string,
  payload: LookupOut,
  opts: LookupOpts = {},
): void {
  cache.set(cacheKey(prefix, opts), payload)
}

export function _clearCacheForTest(): void {
  cache.clear()
  inflight.clear()
}
