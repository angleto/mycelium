import { useCallback, useEffect, useSyncExternalStore } from 'react'
import { api } from '../api/client'
import type { components } from '../shared'
import {
  getSession,
  subscribe as subscribeSession,
  switchWorkspace,
} from './session'
import { initialWorkspaceId } from '../lib/workspaceChoice'
import { useSession } from './useSession'

export type WorkspaceSummary = components['schemas']['WorkspaceSummaryOut']

// The roster of workspaces the signed-in USER belongs to — one shared
// store instead of a fetch per consumer.
//
// It is user-scoped, not workspace-scoped: `GET /workspaces` is a
// pre-tenant call (no X-Workspace-Id), so switching workspace must NOT
// refetch it. It is keyed by the access token instead, which is what
// makes it self-invalidating: log out and back in as someone else and
// the snapshot is dropped rather than leaking the previous account's
// workspace names (several other module caches in the SPA claim a
// logout reset path that does not exist; this one does not rely on
// one).
//
// Every mutation of the roster (create / archive / unarchive / delete)
// goes through `reloadWorkspaces()`, so the sidebar switcher and the
// settings list can never disagree about what exists.

type Snapshot = {
  token: string
  list: WorkspaceSummary[]
}

let cache: Snapshot | null = null
let inflight: Promise<void> | null = null
const listeners = new Set<() => void>()

function emit(): void {
  for (const l of listeners) l()
}

function subscribeStore(l: () => void): () => void {
  listeners.add(l)
  return () => {
    listeners.delete(l)
  }
}

// The snapshot ref must be stable between renders or useSyncExternalStore
// loops: `cache` is only ever replaced, never mutated in place.
function getStore(): Snapshot | null {
  return cache
}

async function fetchList(token: string): Promise<void> {
  const { data } = await api.GET('/workspaces')
  // Only a SUCCESSFUL response is evidence about the roster. On an
  // error (offline, 500, a rotated token) keep what we had: acting on
  // an absent list would be reacting to a network blip.
  if (!data) return
  // Guard against a late response from a previous identity: the token
  // may have rotated (refresh) or the user may have logged out while
  // this was in flight.
  const now = getSession()?.token
  if (!now || now !== token) return
  cache = { token, list: data }
  emit()
  healActiveWorkspace(data)
}

/** The active workspace can stop existing without this tab doing
 * anything: another tab (or another owner) deleted it, or your
 * membership was revoked. The session would keep sending the dead id
 * on every request and the server would keep answering 403 — which is
 * not a 401, so nothing clears it and the app is simply stuck.
 *
 * Whenever an authoritative roster says the id is not ours, move to one
 * that is. An EMPTY roster is deliberately not acted on: a user with no
 * workspace at all is a server-side state this client cannot repair,
 * and logging them out over it would turn a blip into a lockout. */
function healActiveWorkspace(list: WorkspaceSummary[]): void {
  const active = getSession()?.workspaceId
  if (!active || list.length === 0) return
  if (list.some((w) => w.id === active)) return
  const next = initialWorkspaceId(list, null)
  if (next) switchWorkspace(next)
}

/** Refetch the roster and notify every consumer. Awaited by callers
 * that just changed it, so the next render already reflects the new
 * truth. Concurrent calls share one request. */
export async function reloadWorkspaces(): Promise<void> {
  const token = getSession()?.token
  if (!token) return
  if (inflight) return inflight
  inflight = fetchList(token).finally(() => {
    inflight = null
  })
  return inflight
}

/** Drop the cached roster (used on logout, where the token disappears
 * before any consumer can notice it changed). */
export function clearWorkspacesCache(): void {
  cache = null
  emit()
}

// A session that loses its token has logged out: forget the roster
// rather than keep another account's workspace names in memory.
subscribeSession(() => {
  if (!getSession() && cache) clearWorkspacesCache()
})

/** The workspaces the signed-in user belongs to. `null` while the
 * first fetch is in flight — consumers render a placeholder rather
 * than an empty list, which would read as "you have no workspaces". */
export function useWorkspaces(): {
  list: WorkspaceSummary[] | null
  reload: () => Promise<void>
} {
  const session = useSession()
  const token = session?.token ?? null
  const snap = useSyncExternalStore(subscribeStore, getStore)

  useEffect(() => {
    if (!token) return
    if (cache && cache.token === token) return
    void reloadWorkspaces()
  }, [token])

  const reload = useCallback(() => reloadWorkspaces(), [])
  const list = snap && token && snap.token === token ? snap.list : null
  return { list, reload }
}
