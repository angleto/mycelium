import { useEffect, useState } from 'react'
import { api, workspaceHeader } from '../api/client'
import type { components } from '../api/schema'
import { clearOnWorkspaceChange } from './tenantCache'

// Single source of truth for "what is running now". A running timer is
// a server row (ended_at IS NULL); the elapsed shown to the user is
// derived from the server `started_at`, never accumulated client-side.
// So closing the lid, sleeping, reconnecting or reloading cannot drift
// or lose time: on resume we just re-read /time/running and recompute.
// One shared fetch loop + listeners feed every consumer (the top-bar
// chip, every TaskTimer) instead of N independent polls.

type Entry = components['schemas']['TimeEntryOut']

let cache: Entry[] = []
let listenersInstalled = false
let inflight: Promise<void> | null = null
const subs = new Set<() => void>()

// The running entries belong to the workspace they were read in. Drop
// them on a switch so the top-bar chip cannot show the previous
// tenant's timer until the first refetch lands.
clearOnWorkspaceChange(() => {
  cache = []
  for (const s of subs) s()
})

function refresh(): Promise<void> {
  if (inflight) return inflight
  inflight = (async () => {
    try {
      const { data } = await api.GET('/time/running', {
        params: { header: workspaceHeader() },
      })
      cache = data ?? []
      for (const s of subs) s()
    } catch {
      // Keep the last known state: a transient network/auth blip must
      // not blank a timer the server still considers running.
    } finally {
      inflight = null
    }
  })()
  return inflight
}

/** Force an immediate reconciliation with the server (call right
 * after start/stop so the UI reflects server truth without waiting). */
export function refreshRunning(): Promise<void> {
  return refresh()
}

function ensureListeners(): void {
  if (listenersInstalled) return
  listenersInstalled = true
  const wake = () => {
    if (document.visibilityState === 'visible') void refresh()
  }
  // Resume from lid-close / tab-switch / network loss: reconcile at
  // once instead of waiting for the slow backstop poll.
  document.addEventListener('visibilitychange', wake)
  window.addEventListener('online', () => void refresh())
  window.addEventListener('focus', () => void refresh())
  // Backstop only; the event-driven resyncs above are the real
  // recovery path (also catches a stop done on another device).
  window.setInterval(() => {
    if (document.visibilityState === 'visible') void refresh()
  }, 15000)
}

/** Running entries (server truth) + a 1s clock for derived display. */
export function useRunningTimers(): { running: Entry[]; now: number } {
  const [running, setRunning] = useState<Entry[]>(cache)
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    ensureListeners()
    const cb = () => setRunning(cache)
    subs.add(cb)
    void refresh()
    const clock = window.setInterval(() => setNow(Date.now()), 1000)
    return () => {
      subs.delete(cb)
      clearInterval(clock)
    }
  }, [])
  return { running, now }
}
