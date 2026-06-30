import { useEffect, useRef, useState } from 'react'

/**
 * Out-of-band change detector for an open entity, without realtime
 * infrastructure. An MCP tool, the CLI, or another device can write to a
 * note / task you have open; the server bumps a monotonic ``version`` on
 * every write but never pushes, so the SPA would keep showing the stale
 * snapshot until a manual refresh.
 *
 * This re-probes the server on the SAME wake triggers the running-timer
 * poll already trusts (tab focus / visibility regained / network back) —
 * not on a steady interval, because the real workflow is "edit via MCP /
 * agent, switch back to the SPA". The probe owns what "changed" means
 * (it compares the server ``version`` against what the caller holds) and
 * the hook only raises a latched ``stale`` signal. It NEVER refetches
 * the entity itself: applying the refresh (and guarding unsaved edits)
 * is the caller's job, consistent with the no-auto-merge 409 policy.
 */
export function useStaleWatch(opts: {
  /** Watch only while true (e.g. the modal / detail view is open). */
  enabled: boolean
  /** Identity of the watched entity; changing it clears the latch so a
   * signal from a previously-open entity never bleeds into the next. */
  resetKey: string
  /** Cheap server probe; resolve true when a newer version exists. Must
   * swallow its own errors and resolve false on a transient blip so a
   * network hiccup never raises a false "changed elsewhere". */
  probe: () => Promise<boolean>
}): { stale: boolean; reset: () => void } {
  const { enabled, resetKey, probe } = opts
  const [stale, setStale] = useState(false)
  // Keep the latest probe + stale snapshot in refs so the listener
  // effect can stay mounted across renders: re-subscribing whenever the
  // probe identity (or a version bump) changes would risk dropping a
  // wake event mid-flight. Synced in effects (writing a ref during
  // render is unsafe); the listener reads them only on async wake
  // events, which always run after the commit that flushed these.
  const probeRef = useRef(probe)
  const staleRef = useRef(stale)
  const inflight = useRef(false)
  useEffect(() => {
    probeRef.current = probe
  })
  useEffect(() => {
    staleRef.current = stale
  }, [stale])

  useEffect(() => {
    // (Re)entering a fresh entity or reopening: start not-stale so a
    // latch from a previously-open entity cannot carry over.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setStale(false)
    if (!enabled || !resetKey) return
    let cancelled = false
    const check = async () => {
      // Already flagged, a probe in flight, or a hidden tab: nothing to
      // do. The banner is the user's cue; re-probing while stale is just
      // noise (and a wasted GET).
      if (staleRef.current || inflight.current) return
      if (document.visibilityState !== 'visible') return
      inflight.current = true
      try {
        const isStale = await probeRef.current()
        if (!cancelled && isStale) setStale(true)
      } catch {
        // Transient network / auth blip: keep the last (not-stale)
        // state, exactly like the running-timer poll.
      } finally {
        inflight.current = false
      }
    }
    const wake = () => void check()
    document.addEventListener('visibilitychange', wake)
    window.addEventListener('focus', wake)
    window.addEventListener('online', wake)
    return () => {
      cancelled = true
      document.removeEventListener('visibilitychange', wake)
      window.removeEventListener('focus', wake)
      window.removeEventListener('online', wake)
    }
  }, [enabled, resetKey])

  return {
    stale,
    reset: () => setStale(false),
  }
}
