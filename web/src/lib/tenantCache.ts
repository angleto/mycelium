import { getSession, subscribe } from '../auth/session'

// Module-level caches whose keys only mean something INSIDE one
// workspace — a uuid prefix, a task id, "the timers running now" —
// must not survive a switch to another one.
//
// This used not to matter much: switching workspace lived behind a
// settings page almost nobody visited, and several of these modules
// carry a comment claiming "the SPA invalidates on logout" that was
// simply not true (nothing cleared them, ever). With the switcher one
// click away in the sidebar, a stale entry is no longer a curiosity: it
// is another tenant's title rendered in this one.
//
// Registering here is the fix, in one place, so the invariant is stated
// once instead of re-derived per module. Logging out counts as a change
// (the id becomes null), which is what finally makes those comments
// true.

const clears = new Set<() => void>()
let seen: string | null = getSession()?.workspaceId ?? null

subscribe(() => {
  const now = getSession()?.workspaceId ?? null
  if (now === seen) return
  seen = now
  for (const c of clears) c()
})

/** Drop this cache whenever the active workspace changes (or the user
 * signs out). Returns an unregister function; module-level callers
 * simply ignore it. */
export function clearOnWorkspaceChange(clear: () => void): () => void {
  clears.add(clear)
  return () => {
    clears.delete(clear)
  }
}
