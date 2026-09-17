import { describe, expect, it } from 'vitest'
import { possessionOf, possessionsByTask, type Lease } from './leases'

const NOW = Date.parse('2026-09-17T10:00:00+00:00')

function lease(over: Partial<Lease> = {}): Lease {
  return {
    id: 'l1',
    task_id: 't1',
    state_id: 's1',
    holder_worker_id: 'w1',
    holder_label: 'verify-3',
    holder_identity_id: null,
    acquired_at: '2026-09-17T09:00:00+00:00',
    expires_at: '2026-09-17T11:00:00+00:00',
    renewed_at: null,
    released_at: null,
    release_reason: null,
    fence: 1,
    version: 1,
    ...over,
  }
}

describe('possessionOf', () => {
  it('reports a live lease as held', () => {
    expect(possessionOf(lease(), NOW)?.kind).toBe('held')
  })

  it('reports a released lease as nothing at all', () => {
    expect(possessionOf(lease({ released_at: '2026-09-17T09:30:00+00:00' }), NOW)).toBeNull()
  })

  // The case the task-detail banner got wrong: past its deadline the
  // server will hand the task to anybody, so rendering "held by" sends
  // the reader to wait for something that already happened.
  it('separates a lapsed lease from a live one, before the sweep runs', () => {
    const lapsed = lease({ expires_at: '2026-09-17T09:59:00+00:00' })
    expect(lapsed.released_at).toBeNull()
    expect(possessionOf(lapsed, NOW)?.kind).toBe('stale')
  })

  it('has nothing to say about a task with no lease', () => {
    expect(possessionOf(null, NOW)).toBeNull()
    expect(possessionOf(undefined, NOW)).toBeNull()
  })
})

describe('possessionsByTask', () => {
  it('indexes a workspace listing by task and drops what is over', () => {
    const map = possessionsByTask(
      [
        lease({ task_id: 'a' }),
        lease({ task_id: 'b', released_at: '2026-09-17T09:30:00+00:00' }),
        lease({ task_id: 'c', expires_at: '2026-09-17T09:59:00+00:00' }),
      ],
      NOW,
    )
    expect([...map.keys()].sort()).toEqual(['a', 'c'])
    expect(map.get('a')?.kind).toBe('held')
    expect(map.get('c')?.kind).toBe('stale')
  })

  it('keeps the most recent acquisition when history comes along too', () => {
    const map = possessionsByTask(
      [
        lease({ id: 'old', acquired_at: '2026-09-17T08:00:00+00:00', holder_label: 'w1' }),
        lease({ id: 'new', acquired_at: '2026-09-17T09:30:00+00:00', holder_label: 'w2' }),
      ],
      NOW,
    )
    expect(map.get('t1')?.lease.holder_label).toBe('w2')
  })
})
