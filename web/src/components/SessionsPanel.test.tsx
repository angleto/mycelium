import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { SessionsPanel } from './SessionsPanel'
import { possessionsByTask, type Lease } from '../shared'
import '../i18n'

// The view that did not exist anywhere: how many sessions are running,
// and what each one holds. The per-task badge cannot answer it, because
// the interesting case is a session that holds nothing, or one that has
// not been seen for hours and still holds something.

const NOW = Date.parse('2026-09-17T10:00:00+00:00')

let host: HTMLDivElement
let root: Root

function lease(over: Partial<Lease> = {}): Lease {
  return {
    id: 'l1',
    task_id: 't1',
    state_id: 's1',
    holder_worker_id: 'w1',
    holder_label: 'worker-1',
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

const idle = {
  id: 'w2',
  label: 'idle-one',
  opened_at: '2026-09-17T08:00:00+00:00',
  last_seen_at: '2026-09-17T09:55:00+00:00',
  closed_at: null,
}
const busy = {
  id: 'w1',
  label: 'worker-1',
  opened_at: '2026-09-17T08:30:00+00:00',
  last_seen_at: '2026-09-17T09:59:00+00:00',
  closed_at: null,
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

function render(workers: typeof busy[], leases: Lease[]): void {
  act(() =>
    root.render(
      <MemoryRouter>
        <SessionsPanel
          workers={workers}
          possessions={possessionsByTask(leases, NOW)}
          titles={new Map([['t1', 'Rifare il gate']])}
        />
      </MemoryRouter>,
    ),
  )
}

function rows(): HTMLElement[] {
  return [...host.querySelectorAll('.sessions__row')].filter(
    (el): el is HTMLElement => el instanceof HTMLElement,
  )
}

describe('SessionsPanel', () => {
  it('says what each session holds, by name and not by id', () => {
    render([busy], [lease()])
    const link = host.querySelector('.sessions__task')
    expect(link?.textContent).toContain('Rifare il gate')
    expect(link?.getAttribute('href')).toBe('/tasks/t1')
  })

  it('puts the sessions doing something above the ones that are not', () => {
    render([idle, busy], [lease()])
    const [first, second] = rows()
    expect(first.textContent).toContain('worker-1')
    expect(second.textContent).toContain('idle-one')
  })

  it('says outright that a session holds nothing, rather than leaving a blank', () => {
    render([idle], [])
    expect(rows()[0].textContent).toMatch(/niente|nothing/)
  })

  it('takes no room on a workspace where nothing is running', () => {
    render([], [])
    expect(host.querySelector('.sessions')).toBeNull()
  })
})
