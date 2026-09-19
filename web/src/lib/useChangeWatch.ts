import { useEffect, useRef } from 'react'
import { api, workspaceHeader } from '../api/client'

/** Whether a probe may run at all. Everything here is a reason NOT to
 * interrupt: a hidden tab (nobody is looking, and the browser throttles the
 * timer anyway), a drag in flight (re-rendering the board loses the drop),
 * an open ``<select>`` (a re-render closes it under the pointer), or a
 * probe that has not come back yet. */
export function shouldProbe(g: {
  visible: boolean
  dragging: boolean
  selectFocused: boolean
  inflight: boolean
}): boolean {
  return g.visible && !g.dragging && !g.selectFocused && !g.inflight
}

/** What to do with an answer.
 *
 * ``baseline`` takes the number as the starting point and refreshes
 * nothing: the first answer of a watch describes a past the view was
 * already rendered from. ``refresh`` is the only outcome that re-reads, and
 * it needs the count to have MOVED, not merely to be non-zero -- the
 * bootstrap deliberately starts a couple of seconds in the past, so a fresh
 * watch usually starts with entries already inside its window. */
export function decideChangeAction(s: {
  bootstrapped: boolean
  baseline: number
  probed: { since: string; changes: number } | null
}): 'none' | 'baseline' | 'refresh' {
  if (s.probed === null) return 'none'
  if (!s.bootstrapped) return 'baseline'
  return s.probed.changes === s.baseline ? 'none' : 'refresh'
}

/**
 * Keeps an open COLLECTION view in step with a workspace other clients are
 * writing to: an MCP session, the CLI, the worker. Its sibling
 * ``useStaleWatch`` answers the same question for a single open entity and
 * answers it by raising a banner; here there is no entity to be dirty and
 * nothing to discard, so the refresh is taken rather than offered.
 *
 * What it polls is a COUNT, not the data: the server says how many things
 * have happened in the watched scope since an instant it chose itself, and
 * a number that moved is the whole signal. The view is then re-read through
 * the ordinary fetch it already performs, so authorisation, redaction and
 * tenancy are applied on the refresh exactly as on the first load. Nothing
 * arrives by a side channel.
 *
 * Why a poll and not a pushed stream: measured on 200k audit rows, the
 * probe is an index-only scan of 0.22 ms, so ten open browsers at this
 * interval are two queries a second. A stream would buy 5 seconds of
 * latency and cost a second authentication path (a browser WebSocket cannot
 * carry the Bearer header this SPA authenticates with), a grant that
 * outlives the token that opened it, a per-replica connection registry, and
 * an endpoint no generated type can describe. The trade is worth revisiting
 * at a scale this deployment is nowhere near, and the seam for it is right
 * here: the caller passes a ``probe``, so the transport can change without
 * a view knowing.
 */
export function useChangeWatch(opts: {
  /** Watch only while true (the route is mounted and has loaded). */
  enabled: boolean
  /** Clearing this restarts the watch from a fresh baseline: a different
   * workspace has a different activity log and the old count means
   * nothing in it. */
  resetKey: string
  /** Server probe. Called with the instant held from the last bootstrap,
   * or null to bootstrap. MUST swallow its own errors and resolve null on
   * a blip, so a network hiccup is a missed tick and not a refresh storm. */
  probe: (since: string | null) => Promise<{ since: string; changes: number } | null>
  /** Re-read the view. Called only when the count actually moved. */
  onChange: () => void
  /** Milliseconds between probes while the tab is visible. */
  intervalMs?: number
}): void {
  const { enabled, resetKey, probe, onChange, intervalMs = DEFAULT_INTERVAL_MS } = opts
  const probeRef = useRef(probe)
  const onChangeRef = useRef(onChange)
  // The instant the server measured from, and the count that came back with
  // it. The count is a BASELINE, not zero: the bootstrap deliberately starts
  // a couple of seconds in the past (a write already in flight carries a
  // timestamp older than the instant the server hands out), so a fresh
  // watch legitimately starts with entries already in its window.
  const since = useRef<string | null>(null)
  const baseline = useRef<number>(0)
  const inflight = useRef(false)
  // A drag is a gesture, not a state: re-rendering the board out from under
  // a card being dropped loses the drop. Tracked from the platform's own
  // events rather than from the board's state, so the hook does not need to
  // know which component is dragging -- or that there is a board at all.
  const dragging = useRef(false)

  useEffect(() => {
    probeRef.current = probe
    onChangeRef.current = onChange
  })

  useEffect(() => {
    if (!enabled || !resetKey) return
    since.current = null
    baseline.current = 0
    let cancelled = false

    const check = async () => {
      if (cancelled) return
      const guards = {
        visible: document.visibilityState === 'visible',
        dragging: dragging.current,
        // The list row puts a <select> on every line; a filter box is not
        // covered on purpose, since it lives outside the rows and keeps
        // both focus and value across a refetch.
        selectFocused: document.activeElement instanceof HTMLSelectElement,
        inflight: inflight.current,
      }
      if (!shouldProbe(guards)) return
      inflight.current = true
      try {
        const bootstrapped = since.current !== null
        const probed = await probeRef.current(since.current)
        if (cancelled) return
        const action = decideChangeAction({
          bootstrapped,
          baseline: baseline.current,
          probed,
        })
        if (action === 'none') return
        if (action === 'baseline') {
          if (probed) {
            since.current = probed.since
            baseline.current = probed.changes
          }
          return
        }
        // Re-baseline BEFORE re-reading, never after: a write landing
        // between the two would otherwise be swallowed by the new
        // baseline while missing from the data that was just fetched.
        // The worst this order can cost is one redundant refresh.
        const boot = await probeRef.current(null)
        if (cancelled) return
        if (boot) {
          since.current = boot.since
          baseline.current = boot.changes
        }
        onChangeRef.current()
      } catch {
        // Transient network or auth blip: keep the watermark we hold and
        // try again on the next tick, exactly like the running-timer poll.
      } finally {
        inflight.current = false
      }
    }

    const wake = () => void check()
    const onDragStart = () => {
      dragging.current = true
    }
    const onDragEnd = () => {
      dragging.current = false
    }
    document.addEventListener('visibilitychange', wake)
    window.addEventListener('focus', wake)
    window.addEventListener('online', wake)
    document.addEventListener('dragstart', onDragStart)
    document.addEventListener('dragend', onDragEnd)
    document.addEventListener('drop', onDragEnd)
    const timer = window.setInterval(wake, intervalMs)
    void check()
    return () => {
      cancelled = true
      window.clearInterval(timer)
      document.removeEventListener('visibilitychange', wake)
      window.removeEventListener('focus', wake)
      window.removeEventListener('online', wake)
      document.removeEventListener('dragstart', onDragStart)
      document.removeEventListener('dragend', onDragEnd)
      document.removeEventListener('drop', onDragEnd)
    }
  }, [enabled, resetKey, intervalMs])
}

/** Five seconds: fast enough that a board watched while agents work reads
 * as live, slow enough that the probe's cost stays invisible. It is a
 * ceiling on the lag, not the lag itself -- a change also lands on the next
 * tab focus, which is what closes the gap for a window left in the
 * background. */
export const DEFAULT_INTERVAL_MS = 5000

/** The default transport: the REST watermark, for the workspace the rest of
 * the SPA is reading. Separate from the hook so the hook keeps knowing
 * nothing about how the answer arrives. */
export function watermarkProbe(scope: 'tasks') {
  return async (since: string | null): Promise<{ since: string; changes: number } | null> => {
    const { data } = await api.GET('/activity/watermark', {
      params: {
        header: workspaceHeader(),
        query: { scope, ...(since ? { since } : {}) },
      },
    })
    return data ? { since: data.since, changes: data.changes } : null
  }
}
