import { useEffect, useState, useSyncExternalStore } from 'react'
import { api, workspaceHeader } from '../api/client'
import { getSession } from '../auth/session'
import type { components } from '../shared'
import { clearOnWorkspaceChange } from './tenantCache'

// The sessions a person steers (docs/adr/0078), read once for every surface
// that shows them: the top-bar count, the line on the tasks board and the
// sessions page. One store and one poll, for the reason the running-timer
// store gives at length: two copies of one fact disagree, and here the fact
// is "who is waiting for you", which is the one that must not disagree
// between the badge that sends you to the page and the page itself.
//
// Polled, not pushed. There is no push channel in the app, and the page's
// questions are answered at a 15-second grain: a permission prompt is only
// reported by the host after it has waited about six seconds.
type Page = components['schemas']['SessionsPage']

export const SESSIONS_POLL_MS = 15000

export type SessionsState = {
  /** False until a read has SUCCEEDED in the current workspace, so "nobody
   * is waiting" is never shown for "not read yet". */
  known: boolean
  page: Page | null
  /** ``Date.now()`` when ``page`` arrived. Every "since" on screen is
   * measured on the server's clock (``page.now``) plus the time elapsed
   * here since then, so a laptop whose clock is off still reads right. */
  receivedAtMs: number
  /** The last read failed. The page that was read before stays on screen,
   * and says how old it is, rather than turning into an empty list. */
  failed: boolean
}

const UNKNOWN: SessionsState = { known: false, page: null, receivedAtMs: 0, failed: false }

let state: SessionsState = UNKNOWN
let inflight: Promise<void> | null = null
let listenersInstalled = false
const subs = new Set<() => void>()

function publish(next: SessionsState): void {
  state = next
  for (const s of subs) s()
}

function activeWorkspace(): string | null {
  return getSession()?.workspaceId ?? null
}

// A switch drops what was read in the other workspace, and reads again only
// if something is showing sessions: a read nobody asked for is one that can
// land after the next switch.
clearOnWorkspaceChange(() => {
  publish(UNKNOWN)
  if (subs.size > 0) void refreshSessions()
})

async function read(): Promise<void> {
  const ws = activeWorkspace()
  if (ws === null) return
  try {
    const { data, error } = await api.GET('/sessions', {
      params: { header: workspaceHeader(), query: { limit: 200 } },
    })
    if (activeWorkspace() !== ws) return
    if (error || data === undefined) {
      publish({ ...state, failed: true })
      return
    }
    publish({ known: true, page: data, receivedAtMs: Date.now(), failed: false })
  } catch {
    if (activeWorkspace() === ws) publish({ ...state, failed: true })
  }
}

/** Read now, joining a read already on the wire. */
export function refreshSessions(): Promise<void> {
  if (inflight) return inflight
  const p = read().finally(() => {
    if (inflight === p) inflight = null
  })
  inflight = p
  return p
}

function ensureListeners(): void {
  if (listenersInstalled) return
  listenersInstalled = true
  const wake = () => {
    // Nobody is looking at a hidden tab, and the browser throttles its
    // timers anyway: the read waits for the tab to come back.
    if (document.visibilityState === 'visible' && subs.size > 0) void refreshSessions()
  }
  document.addEventListener('visibilitychange', wake)
  window.addEventListener('focus', wake)
  window.addEventListener('online', wake)
  window.setInterval(wake, SESSIONS_POLL_MS)
}

function subscribe(cb: () => void): () => void {
  subs.add(cb)
  return () => {
    subs.delete(cb)
  }
}

/** The sessions page as last read, and a clock that moves every 30 s so
 * the durations on screen move with it. */
export function useSessions(): SessionsState & { nowMs: number } {
  const snap = useSyncExternalStore(subscribe, () => state, () => UNKNOWN)
  const [nowMs, setNowMs] = useState(() => Date.now())
  useEffect(() => {
    ensureListeners()
    void refreshSessions()
    const clock = window.setInterval(() => setNowMs(Date.now()), 30000)
    return () => clearInterval(clock)
  }, [])
  return { ...snap, nowMs }
}

/** The server's "now", advanced by what has elapsed here since the read. */
export function serverNowMs(s: SessionsState, nowMs: number): number {
  if (!s.page) return nowMs
  return new Date(s.page.now).getTime() + Math.max(0, nowMs - s.receivedAtMs)
}

/** Test seam: forget what was read. Never call from app code. */
export function __resetSessions(): void {
  state = UNKNOWN
  inflight = null
}
