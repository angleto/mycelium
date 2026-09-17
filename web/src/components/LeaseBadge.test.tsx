import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { LeaseBadge } from './LeaseBadge'
import { possessionOf, type Lease } from '../shared'
import '../i18n'

// The board's answer to "is this card taken". What is asserted here is
// the distinction the first version of the task-page banner did not
// make: a hold past its deadline is not a hold, and rendering it as one
// tells the reader to wait for something that has already happened.

const NOW = Date.parse('2026-09-17T10:00:00+00:00')

let host: HTMLDivElement
let root: Root

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

beforeEach(() => {
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
})

afterEach(() => {
  act(() => root.unmount())
  host.remove()
})

function badge(): HTMLElement | null {
  const el = host.querySelector('.leasebadge')
  return el instanceof HTMLElement ? el : null
}

describe('LeaseBadge', () => {
  it('names the holder, and carries the deadline where it does not crowd the row', () => {
    act(() => root.render(<LeaseBadge possession={possessionOf(lease(), NOW)} />))
    const el = badge()
    expect(el?.textContent).toContain('verify-3')
    expect(el?.classList.contains('leasebadge--stale')).toBe(false)
    // The "until" belongs in the accessible title: a board is read for
    // taken-or-free, and only somebody deciding whether to wait needs
    // the hour.
    expect(el?.getAttribute('title')).toContain('verify-3')
  })

  it('marks a lapsed hold as its own thing, not as held', () => {
    const lapsed = lease({ expires_at: '2026-09-17T09:59:00+00:00' })
    act(() => root.render(<LeaseBadge possession={possessionOf(lapsed, NOW)} />))
    expect(badge()?.classList.contains('leasebadge--stale')).toBe(true)
  })

  it('renders nothing at all for a task nobody holds', () => {
    act(() => root.render(<LeaseBadge possession={undefined} />))
    expect(badge()).toBeNull()
    act(() => root.render(<LeaseBadge possession={possessionOf(lease({ released_at: '2026-09-17T09:30:00+00:00' }), NOW)} />))
    expect(badge()).toBeNull()
  })
})
