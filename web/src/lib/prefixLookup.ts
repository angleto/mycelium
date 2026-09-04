// The SPA's cache and transport for ``GET /lookup/{prefix}``.
//
// What a code IS, which perimeter a caller is asking in, and how the
// request is built live in web/src/shared/prefix.ts, because the
// extension resolves the same codes against the same endpoint. What
// lives here is what only the SPA has: an in-process cache and the
// tenant-change invalidation that keeps it honest.
//
// Module-level, so the prefix that occurs N times in one rendered
// document collapses to one round trip. We intentionally do NOT
// coalesce via React state: the renderer mounts components
// synchronously, so concurrent calls all see ``inflight`` and await the
// same promise without spurious traffic.
//
// Entries are not expired in-process: an archive/delete upgrade or a
// title edit is eventually consistent for chip rendering, and a refresh
// re-reads. The cache IS dropped whenever the active workspace changes,
// including on logout, because a prefix is only unique inside one
// tenant: an entry resolved in workspace A would otherwise route a chip
// in workspace B to an entity that is not there.

import { RESOLVE_ID, lookupCacheKey, lookupPath } from '../shared'
import type { LookupMatch, LookupOpts, LookupOut } from '../shared'
import { authFetch } from '../api/client'
import { clearOnWorkspaceChange } from './tenantCache'

export type { LookupMatch, LookupOpts, LookupOut }
export { RESOLVE_ID }
export { isFullUuid, isPrefixCandidate } from '../shared'

const cache = new Map<string, LookupOut>()
const inflight = new Map<string, Promise<LookupOut | null>>()

clearOnWorkspaceChange(() => {
  cache.clear()
  inflight.clear()
})

export function getCachedLookup(prefix: string, opts: LookupOpts = {}): LookupOut | undefined {
  return cache.get(lookupCacheKey(prefix, opts))
}

export async function lookupPrefix(
  prefix: string,
  opts: LookupOpts & { signal?: AbortSignal } = {},
): Promise<LookupOut | null> {
  const key = lookupCacheKey(prefix, opts)
  const hit = cache.get(key)
  if (hit) return hit
  const pending = inflight.get(key)
  if (pending) return pending
  const p = (async () => {
    const res = await authFetch(lookupPath(prefix, opts), { signal: opts.signal })
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
  cache.set(lookupCacheKey(prefix, opts), payload)
}

export function _clearCacheForTest(): void {
  cache.clear()
  inflight.clear()
}
