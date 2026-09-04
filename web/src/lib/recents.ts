// The SPA's localStorage binding for the recently-opened list.
//
// The rules -- workspace scoping, the cap, the dedupe by route, the
// refusal to record a row whose title has not resolved yet -- live in
// web/src/shared/recents.ts, because the extension keeps the same list
// against a different store and the rules are where the mistakes are.
// What lives here is the store itself and the answer to "which workspace
// am I in", both of which are the SPA's alone.
//
// Recorded from two places: the palette (every navigation it drives) and
// the task detail route (every task opened). Note detail visits outside
// the palette are not yet recorded.

import {
  RECENTS_LEGACY_FLAT_KEY,
  type RecentItem,
  parseRecents,
  recentsKey,
  withRecent,
} from '../shared'
import { getSession } from '../auth/session'

export type { RecentItem }

function storageKey(): string | null {
  return recentsKey(getSession()?.workspaceId)
}

// The flat key predates workspace scoping and is dropped rather than
// migrated: folding it into whichever workspace happens to be active is
// exactly the cross-tenant leak that scoping fixed.
try {
  localStorage.removeItem(RECENTS_LEGACY_FLAT_KEY)
} catch {
  /* private mode: nothing to clean */
}

export function getRecents(): RecentItem[] {
  try {
    const key = storageKey()
    if (!key) return []
    return parseRecents(localStorage.getItem(key))
  } catch {
    return []
  }
}

export function pushRecent(item: RecentItem): void {
  try {
    const key = storageKey()
    if (!key) return
    localStorage.setItem(key, JSON.stringify(withRecent(getRecents(), item)))
  } catch {
    /* private mode / quota: recents are best-effort */
  }
}
