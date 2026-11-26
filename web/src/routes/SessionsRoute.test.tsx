import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { SessionsRoute } from './SessionsRoute'
import { SessionsAttention, SessionsSummary } from '../components/SessionsAttention'
import { __resetSessions, refreshSessions } from '../lib/useSessions'
import { clearSession, setSession } from '../auth/session'
import type { components } from '../shared'
import '../i18n'
import i18n from '../i18n'

// What the sessions page promises a person (docs/adr/0078), against a double
// of GET /sessions that answers the server's contract:
// - who is waiting comes first, in the server's order, each in words;
// - the tasks a session holds are links, and no id is ever shown;
// - sessions that went quiet are folded away, not mixed in;
// - with nothing reporting, the page says how to connect a machine;
// - a failed refresh keeps what was read and says so, instead of emptying;
// - the top-bar count exists only while somebody waits.

const fetchMock = vi.hoisted(() => {
  const fn = vi.fn<(...args: unknown[]) => Promise<Response>>()
  globalThis.fetch = fn as unknown as typeof globalThis.fetch
  return fn
})

type Page = components['schemas']['SessionsPage']
type Row = components['schemas']['SessionRowOut']

const NOW = '2026-11-26T12:00:00Z'
const ago = (sec: number) => new Date(Date.parse(NOW) - sec * 1000).toISOString()
const UUID_RE = /[0-9a-f]{8}-[0-9a-f]{4}-/

function row(id: string, over: Partial<Row>): Row {
  return {
    session_id: `00000000-0000-4000-8000-0000000000${id}`,
    worker_ids: [`00000000-0000-4000-8000-0000000001${id}`],
    agent: 'claude_code',
    project: null,
    machine: 'lab',
    title: null,
    label: null,
    attention: 'working',
    blocked_on: null,
    attention_since: ago(600),
    opened_at: ago(7200),
    heard_at: ago(10),
    group: 'working',
    held: [],
    ...over,
  }
}

function page(rows: Row[]): Page {
  const count = (g: Row['group']) => rows.filter((r) => r.group === g).length
  return {
    sessions: rows,
    counts: { needs_you: count('needs_you'), working: count('working'), quiet: count('quiet') },
    quiet_after_seconds: 900,
    now: NOW,
  }
}

const FULL = page([
  row('a1', {
    project: 'symbolum',
    attention: 'blocked',
    blocked_on: 'permission',
    attention_since: ago(12 * 60),
    group: 'needs_you',
  }),
  row('a2', {
    project: 'kiwi',
    attention: 'your_turn',
    attention_since: ago(3 * 3600),
    group: 'needs_you',
    held: [
      {
        task_id: '00000000-0000-4000-8000-00000000f001',
        title: 'Fix the lease sweep',
        expires_at: ago(-600),
      },
    ],
  }),
  row('a3', { project: 'humus', attention_since: ago(40 * 60) }),
  row('a4', { session_id: null, agent: 'unknown', attention: null, attention_since: null, label: 'other-client' }),
  row('a5', { project: 'mycelium', group: 'quiet', heard_at: ago(25 * 60) }),
])

let answer: () => Promise<Response>
const ok = (p: Page) => () =>
  Promise.resolve(new Response(JSON.stringify(p), { status: 200, headers: { 'content-type': 'application/json' } }))
const fail = () => Promise.resolve(new Response('{}', { status: 502 }))

let host: HTMLDivElement
let root: Root

async function settle(): Promise<void> {
  await act(async () => {
    await new Promise((r) => setTimeout(r, 0))
  })
}

async function mount(node: React.ReactNode): Promise<void> {
  await act(async () => {
    root.render(<MemoryRouter>{node}</MemoryRouter>)
  })
  await settle()
}

const text = (sel: string) => [...host.querySelectorAll(sel)].map((e) => e.textContent ?? '')

beforeEach(async () => {
  await i18n.changeLanguage('en')
  setSession({ token: 't', workspaceId: '00000000-0000-4000-8000-000000000001' })
  __resetSessions()
  answer = ok(FULL)
  fetchMock.mockImplementation(() => answer())
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
})

afterEach(() => {
  act(() => root.unmount())
  host.remove()
  fetchMock.mockReset()
  clearSession()
})

describe('SessionsRoute', () => {
  it('puts who waits for the person first, each state in words', async () => {
    await mount(<SessionsRoute />)
    expect(text('h2')).toEqual(['Waiting for you · 2', 'Working · 2'])
    const needsYou = host.querySelector('section[aria-labelledby="sessions-needs-you"]')!
    expect([...needsYou.querySelectorAll('.sessionrow__name')].map((e) => e.textContent)).toEqual([
      'symbolum',
      'kiwi',
    ])
    expect(needsYou.textContent).toContain('Waiting for a permission · 12 min')
    expect(needsYou.textContent).toContain('Finished, waiting for your next instruction · 3 h')
    expect(host.textContent).toContain('Working · 40 min')
    expect(host.textContent).toContain('Not reporting (another client)')
  })

  it('links the tasks a session holds and never shows an id', async () => {
    await mount(<SessionsRoute />)
    const link = [...host.querySelectorAll('a')].find((a) => a.textContent === 'Fix the lease sweep')
    expect(link?.getAttribute('href')).toBe('/tasks/00000000-0000-4000-8000-00000000f001')
    // Hrefs may carry ids; what a person reads must not.
    expect(host.textContent).not.toMatch(UUID_RE)
  })

  it('folds the sessions that went quiet away from the working ones', async () => {
    await mount(<SessionsRoute />)
    const quiet = host.querySelector('details')!
    expect(quiet.open).toBe(false)
    expect(quiet.querySelector('summary')?.textContent).toBe('Gone quiet · 1')
    expect(quiet.textContent).toContain('No news for 25 min')
  })

  it('tells a person with no reporting session how to connect a machine', async () => {
    answer = ok(page([]))
    await mount(<SessionsRoute />)
    expect(host.querySelector('code')?.textContent).toBe('mycelium session install-hooks --apply')
    expect(host.querySelectorAll('h2')).toHaveLength(0)
  })

  it('keeps what it read when a refresh fails, and says so', async () => {
    await mount(<SessionsRoute />)
    answer = fail
    await act(async () => {
      await refreshSessions()
    })
    expect(host.querySelector('[role="status"]')?.textContent).toMatch(/latest refresh failed/)
    expect(host.textContent).toContain('symbolum')
  })

  it('says it could not read rather than claiming nobody is waiting', async () => {
    answer = fail
    await mount(<SessionsRoute />)
    expect(host.querySelector('[role="status"]')?.textContent).toMatch(/Could not read/)
    expect(host.textContent).not.toContain('Nobody is waiting for you')
  })
})

describe('SessionsAttention', () => {
  it('shows the count, named for assistive technology, and marks a blocked one', async () => {
    await mount(<SessionsAttention />)
    const badge = host.querySelector('a[href="/sessions"]')!
    expect(badge.getAttribute('aria-label')).toBe('2 sessions are waiting for you')
    expect(badge.className).toContain('sessionsbadge--blocked')
  })

  it('is absent while nobody waits', async () => {
    answer = ok(page([row('b1', { project: 'humus' })]))
    await mount(<SessionsAttention />)
    expect(host.querySelector('a')).toBeNull()
  })
})

describe('SessionsSummary', () => {
  it('is one line on the board, linking to the page', async () => {
    await mount(<SessionsSummary />)
    const link = host.querySelector('a[href="/sessions"]')!
    expect(link.textContent).toBe('5 sessions · 2 waiting for you →')
  })
})
