// The recently-opened list, as a contract rather than a store.
//
// Two clients keep one: the SPA's command palette and the extension's
// panel. They cannot share the storage itself -- an extension cannot
// read the app origin's localStorage, and even if it could, one key
// serving two lifecycles is how the bug below happened -- but they must
// share the rules, because the rules are where the mistakes are.
//
// PER WORKSPACE, not per browser. The rows carry TITLES, so one flat key
// meant the palette in workspace B listed the notes and tasks you had
// opened in workspace A: visible cross-tenant leakage, and the ids would
// not have resolved there anyway. Every key this module produces is
// therefore workspace-scoped, and a caller that cannot name a workspace
// gets no key at all rather than a shared one.
//
// Best-effort by design. A parse failure, a quota failure or private
// browsing degrades to "no recents" and never throws: a decorative list
// must not be able to break the surface it decorates.
//
// Pure by contract: this directory is compiled into more than one
// package and must import nothing. Storage is the caller's -- localStorage
// in the SPA, chrome.storage.local in the extension -- and each supplies
// the workspace id it is currently scoped to.

export interface RecentItem {
  kind: 'task' | 'note'
  id: string
  title: string
  route: string
}

/** Eight is what fits a palette section without scrolling it. */
export const RECENTS_MAX = 8

const RECENTS_PREFIX = 'mycelium.recents.v1'

/** The flat key that predates workspace scoping. Callers drop it on
 *  load rather than migrating it: folding it into whichever workspace
 *  happens to be active is exactly the leak being fixed, and at most
 *  eight rows are lost once. */
export const RECENTS_LEGACY_FLAT_KEY = RECENTS_PREFIX

export function recentsKey(workspaceId: string | null | undefined): string | null {
  return workspaceId ? `${RECENTS_PREFIX}:${workspaceId}` : null
}

export function isRecentItem(x: unknown): x is RecentItem {
  if (!x || typeof x !== 'object') return false
  const r = x as Record<string, unknown>
  return (
    (r.kind === 'task' || r.kind === 'note') &&
    typeof r.id === 'string' &&
    typeof r.title === 'string' &&
    typeof r.route === 'string'
  )
}

/** Whatever was in storage, as a list this code is willing to render.
 *  Anything unrecognised is dropped item by item rather than failing the
 *  whole list, so one malformed row written by an older build does not
 *  empty the section. */
export function parseRecents(raw: string | null | undefined): RecentItem[] {
  if (!raw) return []
  try {
    const parsed: unknown = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    return parsed.filter(isRecentItem).slice(0, RECENTS_MAX)
  } catch {
    return []
  }
}

/** The list after opening ``item``: most recent first, deduped by route
 *  so revisiting something moves it rather than repeating it, capped.
 *
 *  An empty title returns the list unchanged. That is not tidiness: an
 *  empty title is the loading placeholder, and recording it writes a row
 *  that renders as a blank line until the entity resolves -- and it
 *  never resolves, because what was stored is the blank. */
export function withRecent(list: readonly RecentItem[], item: RecentItem): RecentItem[] {
  if (!item.title.trim()) return [...list]
  return [item, ...list.filter((r) => r.route !== item.route)].slice(0, RECENTS_MAX)
}
