import { useEffect, useState } from 'react'
import { possessionOf, possessionsForBoard, type Lease, type Possession } from '../shared'

// Reading the clock is not a render-time act: React may re-render at any
// moment, and a verdict computed from ``Date.now()`` mid-render is a
// different answer each time for reasons the component cannot see. So
// the clock is state, and this hook decides WHEN it moves.
//
// Not a poll. The only instant at which a possession's verdict changes
// by itself is the earliest deadline among the leases in hand, so that
// is the single timer scheduled: a board holding nothing schedules
// nothing, and a board with holds re-renders once, when the first one
// lapses. A 30-second interval would have re-reconciled a thousand rows
// forty times an hour to change nothing.
function useDeadlineClock(leases: Lease[]): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    let next = Infinity
    for (const l of leases) {
      if (l.released_at) continue
      const at = Date.parse(l.expires_at)
      if (Number.isFinite(at) && at > now && at < next) next = at
    }
    if (!Number.isFinite(next)) return
    // A second past it, so the comparison it wakes for is already true.
    const id = window.setTimeout(() => setNow(Date.now()), next - now + 1000)
    return () => window.clearTimeout(id)
  }, [leases, now])
  return now
}

/** One fact per task -- who holds it, or who passed it on -- kept true
 *  as deadlines pass. The handoffs move nothing by themselves: they are
 *  already released, so only the live half needs the clock. */
export function usePossessions(leases: Lease[], handedOff: Lease[] = EMPTY): Map<string, Possession> {
  const now = useDeadlineClock(leases)
  return possessionsForBoard(leases, handedOff, now)
}

/** The same question about one task. */
export function usePossession(lease: Lease | null): Possession | null {
  const now = useDeadlineClock(lease ? [lease] : EMPTY)
  return possessionOf(lease, now)
}

// A stable identity, so the effect above is not re-armed on every render
// of a task nobody holds.
const EMPTY: Lease[] = []
