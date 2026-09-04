import { useEffect, useState, useSyncExternalStore } from 'react'
import { api, workspaceHeader } from '../api/client'
import { getSession } from '../auth/session'
import type { components } from '../shared'
import { clearOnWorkspaceChange } from './tenantCache'

// Single source of truth for "what is running now". A running timer is
// a server row (ended_at IS NULL); the elapsed shown to the user is
// derived from the server `started_at`, never accumulated client-side.
// So closing the lid, sleeping, reconnecting or reloading cannot drift
// or lose time: on resume we just re-read /time/running and recompute.
//
// EVERY surface reads this one store: the top-bar chip, each TaskTimer,
// and the Time view's "running now" card. The Time view used to hold its
// own copy behind its own poll and reconcile only that copy after
// start/stop/pause, so a timer started there stayed invisible in the
// top-bar chip until the chip's own backstop happened to fire, and the
// two surfaces disagreed for the length of that window. Two copies of
// one fact disagree eventually; there is one.
type Entry = components['schemas']['TimeEntryOut']

export type RunningState = {
  // False until a read has SUCCEEDED in the current workspace. A caller
  // can therefore tell "no timer is running" from "not read yet" and
  // stop rendering the second as the first.
  known: boolean
  running: Entry[]
}

// One cadence for every consumer, matching what the Time view promises
// in `time.realtimeNote`. Per-consumer cadences (15s chip, 5s Time view)
// were the previous design and are what made the disagreement between
// them visible. This bounds only the OUT-OF-BAND case (a stop from
// another device, an MCP agent, Telegram): every local mutation
// reconciles immediately through refreshRunning(), and there is no push
// channel in v1, so an external change is up to POLL_MS late.
const POLL_MS = 5000

const UNKNOWN: RunningState = { known: false, running: [] }

let state: RunningState = UNKNOWN
let listenersInstalled = false
// The read currently on the wire, and a read queued behind it. See
// refreshRunning() for why a caller sometimes must not join the former.
let inflight: Promise<void> | null = null
let queued: Promise<void> | null = null
const subs = new Set<() => void>()

function publish(next: RunningState): void {
  state = next
  for (const s of subs) s()
}

function activeWorkspace(): string | null {
  return getSession()?.workspaceId ?? null
}

// The running entries belong to the workspace they were read in. Drop
// them on a switch so the top-bar chip cannot show the previous
// tenant's timer, and read again at once: leaving an empty list behind
// would make every surface assert "nothing is running" in the new
// workspace until the next poll, which is a claim we have not checked.
clearOnWorkspaceChange(() => {
  publish(UNKNOWN)
  void refreshRunning()
})

async function read(): Promise<void> {
  const ws = activeWorkspace()
  try {
    const { data, error } = await api.GET('/time/running', {
      params: { header: workspaceHeader() },
    })
    // A read that did not succeed NEVER becomes "nothing is running".
    // openapi-fetch resolves a non-2xx as `{ error }` instead of
    // throwing, so the earlier `cache = data ?? []` turned every 500 /
    // 502 / expired-session 401 into an empty list and blanked the
    // top-bar chip while the timer kept running on the server. Only a
    // rejected fetch (the network actually down) ever reached the catch
    // below, which is the one case that code was written for.
    if (error || data === undefined) return
    // Answer to a question asked in another tenant: the switch already
    // reset the store, and publishing this would put the previous
    // workspace's timer back on screen.
    if (activeWorkspace() !== ws) return
    publish({ known: true, running: data })
  } catch {
    // Thrown rather than returned: fetch rejected (offline), or
    // workspaceHeader() found no session. Same policy as above, keep
    // the last state we actually read.
    return
  }
}

/** Background read: coalesces onto whatever is already on the wire. */
function poll(): Promise<void> {
  if (inflight) return inflight
  const p = read().finally(() => {
    if (inflight === p) inflight = null
  })
  inflight = p
  return p
}

/** Reconcile with the server after a mutation (start / stop / pause /
 * resume), so every consumer reflects server truth without waiting for
 * the next poll.
 *
 * Deliberately not `poll()`: joining a read that left BEFORE the
 * mutation landed would reconcile against a pre-mutation answer and
 * leave the just-started timer missing from every surface until the
 * poll after that. Callers arriving while a read is on the wire share
 * one new read queued behind it, which is issued after they arrived. */
export function refreshRunning(): Promise<void> {
  if (!inflight) return poll()
  if (queued) return queued
  const q = inflight.then(() => {
    if (queued === q) queued = null
    return poll()
  })
  queued = q
  return q
}

function ensureListeners(): void {
  if (listenersInstalled) return
  listenersInstalled = true
  const wake = () => {
    if (document.visibilityState === 'visible') void poll()
  }
  // Resume from lid-close / tab-switch / network loss: reconcile at
  // once instead of waiting for the next poll.
  document.addEventListener('visibilitychange', wake)
  window.addEventListener('online', () => void poll())
  window.addEventListener('focus', () => void poll())
  window.setInterval(wake, POLL_MS)
}

function subscribeStore(cb: () => void): () => void {
  subs.add(cb)
  return () => {
    subs.delete(cb)
  }
}

/** Running entries (server truth), whether they have been read at all,
 * and a 1s clock for the derived display. */
export function useRunningTimers(): RunningState & { now: number } {
  const snap = useSyncExternalStore(
    subscribeStore,
    () => state,
    () => UNKNOWN,
  )
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    ensureListeners()
    void poll()
    const clock = window.setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(clock)
  }, [])
  return { known: snap.known, running: snap.running, now }
}

/** Test seam: forget what was read and any read in flight. Never call
 * from app code. */
export function __resetRunningTimers(): void {
  state = UNKNOWN
  inflight = null
  queued = null
}
