import type { components } from './schema'

export type Lease = components['schemas']['LeaseOut']

/** A live possession, and whether the server would still honour it.
 *
 *  `held` is somebody working on the task now. `stale` is a lease whose
 *  deadline has passed and which the sweep has not reclaimed yet: the
 *  task is free for anybody to take, and saying "held" about it sends
 *  the reader off to wait for something that has already happened.
 *  `handoff` is nobody working on it and a name for who passed it here,
 *  which is what decides whether the reader should be the one to pick it
 *  up: the check is done by somebody other than whoever did the work. */
export type Possession =
  | { kind: 'held'; lease: Lease }
  | { kind: 'stale'; lease: Lease }
  | { kind: 'handoff'; lease: Lease }

/** What a lease says about its task at `now` (epoch ms).
 *
 *  This mirrors `TaskLease.is_live` on the server, deliberately and in
 *  one place. The unique index cannot read the clock -- it excludes on
 *  `released_at IS NULL` alone -- so comparing the deadline is the
 *  reader's job, on both sides. The first version of the task-detail
 *  banner filtered on `released_at` only, which showed a possession the
 *  server already considered reclaimable for as long as the sweep took
 *  to come round. */
export function possessionOf(lease: Lease | null | undefined, now: number): Possession | null {
  if (!lease || lease.released_at) return null
  const until = Date.parse(lease.expires_at)
  // An unparseable deadline is treated as held rather than free: the
  // failure that costs something is two sessions on one task, not a
  // badge that lingers.
  if (Number.isNaN(until)) return { kind: 'held', lease }
  return until > now ? { kind: 'held', lease } : { kind: 'stale', lease }
}

/** One fact per task, chosen by what the reader can act on.
 *
 *  A card can be both held now and handed here earlier, and showing both
 *  is what turns a board into a wall of chips. They are not equally
 *  useful at the same moment: while somebody holds it, the provenance
 *  changes nothing the reader can do, because the card cannot be taken
 *  anyway; once it is free, provenance is the whole decision. So live
 *  possession wins, and the handoff is what a free card says instead of
 *  saying nothing. */
export function possessionsForBoard(
  live: Lease[],
  handedOff: Lease[],
  now: number,
): Map<string, Possession> {
  const out = possessionsByTask(live, now)
  for (const lease of handedOff) {
    if (out.has(lease.task_id)) continue
    if (!lease.released_at) continue
    out.set(lease.task_id, { kind: 'handoff', lease })
  }
  return out
}

/** Index one workspace-wide listing by task, for a board that asks
 *  "which of these is taken" once instead of once per row.
 *
 *  A task has at most one unreleased lease (`uq_task_leases_live`), so
 *  the map cannot lose anything; the most recently acquired row wins
 *  anyway, which is what a caller that passed `include_released` would
 *  mean. */
export function possessionsByTask(rows: Lease[], now: number): Map<string, Possession> {
  const out = new Map<string, Possession>()
  const acquired = new Map<string, number>()
  for (const row of rows) {
    const p = possessionOf(row, now)
    if (!p) continue
    const at = Date.parse(row.acquired_at)
    const seen = acquired.get(row.task_id)
    if (seen !== undefined && !(at > seen)) continue
    acquired.set(row.task_id, at)
    out.set(row.task_id, p)
  }
  return out
}
