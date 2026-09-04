import { useCallback, useEffect, useSyncExternalStore } from 'react'
import { api, errMessage, workspaceHeader } from '../api/client'
import type { components } from '../shared'
import i18n from '../i18n'
import { useSession } from './useSession'

export type Ws = components['schemas']['WorkspaceOut']
type SettingsIn = components['schemas']['WorkspaceSettingsIn']

/** A partial settings write: everything except the bookkeeping fields,
 * which the store owns. `estimate_presets` is REQUIRED by the endpoint
 * but optional here — a caller saving the retrieval floor should not
 * have to restate the estimate presets, and the store fills them from
 * the snapshot it already holds. */
export type SettingsPatch = Partial<Omit<SettingsIn, 'expected_version'>>

export type SaveResult = { ok: true } | { ok: false; message: string }

// The ACTIVE workspace (identity + role + the settings bag), held once
// for the whole SPA.
//
// It used to be a plain module variable read by one hook, while four
// settings panels each fetched `/workspaces/me` for themselves and kept
// a PRIVATE copy of `version`. Since every settings write bumps that
// version, whichever panel saved second got a 409 — on a page where all
// of them are mounted at the same time. The fix is not a retry per
// panel: it is a single snapshot with a single writer, which is what
// this module is. `saveWorkspaceSettings` is the only PATCH path, so
// the version it sends is always the one the last write returned.
//
// Keyed by the active workspace id, so switching workspace drops the
// previous tenant's settings instead of showing them under a new name.

type Snapshot = { wsId: string; ws: Ws }

let cache: Snapshot | null = null
// Keyed by workspace id: an in-flight fetch for the workspace we just
// LEFT must not be handed to a caller asking about the new one.
let inflight: { wsId: string; p: Promise<void> } | null = null
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

// Replaced, never mutated: useSyncExternalStore needs a stable ref.
function getStore(): Snapshot | null {
  return cache
}

async function fetchWs(wsId: string): Promise<void> {
  const { data } = await api.GET('/workspaces/me', {
    params: { header: workspaceHeader() },
  })
  if (!data) return
  // A late response from the workspace we just left must not overwrite
  // the one we are now in.
  if (data.id !== wsId) return
  cache = { wsId, ws: data }
  emit()
}

/** Refetch the active workspace and notify every consumer. Concurrent
 * calls share one request. */
export async function reloadMyWorkspace(): Promise<void> {
  const wsId = activeWorkspaceId()
  if (!wsId) return
  if (inflight && inflight.wsId === wsId) return inflight.p
  const p = fetchWs(wsId).finally(() => {
    if (inflight && inflight.wsId === wsId) inflight = null
  })
  inflight = { wsId, p }
  return p
}

function activeWorkspaceId(): string | null {
  try {
    return workspaceHeader()['x-workspace-id']
  } catch {
    // No session: signed out mid-flight.
    return null
  }
}

/** Write into the workspace settings bag.
 *
 * Owns the two things every caller used to get subtly wrong: the
 * `expected_version` (read from the shared snapshot, so panels cannot
 * hold stale ones) and the required `estimate_presets` (restated from
 * the snapshot unless the caller is the one changing them). A 409 means
 * someone else wrote in between — refresh and retry ONCE, because the
 * merge is server-side and key-wise, so a retry cannot clobber the
 * other write, it just lands after it. */
export async function saveWorkspaceSettings(
  patch: SettingsPatch,
): Promise<SaveResult> {
  if (!cache) await reloadMyWorkspace()
  const loaded = cache
  if (!loaded) return { ok: false, message: i18n.t('error.generic') }

  const attempt = async (): Promise<{ status: number; error: unknown }> => {
    const snap = (cache ?? loaded).ws
    const { error, response } = await api.PATCH('/workspaces/me/settings', {
      params: { header: workspaceHeader() },
      body: {
        ...patch,
        expected_version: snap.version,
        estimate_presets:
          patch.estimate_presets ?? snap.settings?.estimate_presets ?? [],
      },
    })
    return { status: response.status, error: error ?? null }
  }


  let res = await attempt()
  if (res.status === 409) {
    await reloadMyWorkspace()
    res = await attempt()
  }
  if (res.status === 409) {
    await reloadMyWorkspace()
    return { ok: false, message: i18n.t('wsmgr.conflict') }
  }
  if (res.error) return { ok: false, message: errMessage(res.error) }
  await reloadMyWorkspace()
  return { ok: true }
}

/** The active workspace, or `null` while the first fetch is in flight. */
export function useMyWorkspace(): {
  ws: Ws | null
  reload: () => Promise<void>
} {
  const session = useSession()
  const wsId = session?.workspaceId ?? null
  const snap = useSyncExternalStore(subscribeStore, getStore)

  useEffect(() => {
    if (!wsId) return
    if (cache && cache.wsId === wsId) return
    void reloadMyWorkspace()
  }, [wsId])

  const reload = useCallback(() => reloadMyWorkspace(), [])
  const ws = snap && wsId && snap.wsId === wsId ? snap.ws : null
  return { ws, reload }
}
