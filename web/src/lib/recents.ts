// Recently-visited tasks / notes, for the Cmd+K palette's "Recent"
// section (ADR-0038 analysis item D). Stored in localStorage so it
// survives reloads; capped and most-recent-first. Pure client state:
// it never round-trips to the server and is best-effort (a parse or
// quota error degrades to "no recents", never throws).
//
// Recorded from two places: the palette itself (every navigation it
// drives) and the task detail route (every task opened). Note detail
// visits outside the palette are not yet recorded; see the task notes.

import { getSession } from '../auth/session'

export interface RecentItem {
  kind: 'task' | 'note'
  id: string
  title: string
  route: string
}

// Per WORKSPACE, not per browser. The rows carry TITLES, so one flat
// key meant the palette in workspace B listed the notes and tasks you
// had opened in workspace A — visible cross-tenant leakage, and the ids
// would not have resolved there anyway. The old flat key is dropped on
// load rather than migrated: folding it into whichever workspace
// happens to be active is exactly the thing being fixed, and at most
// eight recent entries are lost once.
const KEY = 'mycelium.recents.v1'
const LEGACY_FLAT = KEY
const MAX = 8

function storageKey(): string | null {
  const ws = getSession()?.workspaceId
  return ws ? `${KEY}:${ws}` : null
}

try {
  localStorage.removeItem(LEGACY_FLAT)
} catch {
  /* private mode: nothing to clean */
}

function isRecent(x: unknown): x is RecentItem {
  if (!x || typeof x !== 'object') return false
  const r = x as Record<string, unknown>
  return (
    (r.kind === 'task' || r.kind === 'note') &&
    typeof r.id === 'string' &&
    typeof r.title === 'string' &&
    typeof r.route === 'string'
  )
}

export function getRecents(): RecentItem[] {
  try {
    const key = storageKey()
    if (!key) return []
    const raw = localStorage.getItem(key)
    if (!raw) return []
    const parsed: unknown = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    return parsed.filter(isRecent).slice(0, MAX)
  } catch {
    return []
  }
}

export function pushRecent(item: RecentItem): void {
  // An empty title is the loading placeholder; don't record a row that
  // would render as a blank line until the entity resolves.
  if (!item.title.trim()) return
  try {
    const key = storageKey()
    if (!key) return
    const next = [item, ...getRecents().filter((r) => r.route !== item.route)]
    localStorage.setItem(key, JSON.stringify(next.slice(0, MAX)))
  } catch {
    /* private mode / quota: recents are best-effort */
  }
}
