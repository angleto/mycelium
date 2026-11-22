import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import i18n from 'i18next'
import { DiaryRoute } from './DiaryRoute'
import { digestMarkdown, digestWindow, type WorkDigest } from '../api/workDigest'
import { clearSession, setSession } from '../auth/session'
import '../i18n'

// What the work diary promises (ADR-0070), against a double of the API:
// - the window sent is the local calendar days picked, as instants with an
//   offset, end exclusive at the next local midnight;
// - picking a client narrows the request to it;
// - every event comes out, in the page and in the Markdown, with its text;
// - a withheld section is said, and an empty period says so.

const fetchMock = vi.hoisted(() => {
  const fn = vi.fn<(...args: unknown[]) => Promise<Response>>()
  globalThis.fetch = fn as unknown as typeof globalThis.fetch
  return fn
})

const CLIENT = '00000000-0000-4000-8000-0000000000c1'
const TASK = '00000000-0000-4000-8000-0000000000a1'

const DIGEST: WorkDigest = {
  start_from: '2026-11-09T00:00:00Z',
  start_to: '2026-11-16T00:00:00Z',
  withheld: [],
  seconds: 5400,
  billable_seconds: 5400,
  tasks: [
    {
      task_id: TASK,
      title: 'Ingest schema',
      state: 'in_progress',
      state_is_terminal: false,
      is_archived: false,
      created_at: '2026-11-01T00:00:00Z',
      description: 'agreed with the client',
      project_name: 'R&R',
      client_name: 'Cylock',
      seconds: 5400,
      billable_seconds: 5400,
      events: [
        {
          at: '2026-11-10T09:00:00Z',
          kind: 'state_changed',
          from_state: 'todo',
          to_state: 'in_progress',
          actor: 'angelo',
        },
        {
          at: '2026-11-10T10:00:00Z',
          kind: 'time_entry',
          duration_seconds: 5400,
          billable: true,
          text: 'migrated the ingest\nand ran the backfill',
          actor: 'angelo',
        },
        {
          at: '2026-11-11T08:00:00Z',
          kind: 'comment',
          text: 'schema v2 signed off',
          actor: 'claude',
          via_agent: true,
        },
      ],
    },
  ],
}

let answerDigest: WorkDigest = DIGEST
const digestCalls: URLSearchParams[] = []

function json(body: unknown): Promise<Response> {
  return Promise.resolve(
    new Response(JSON.stringify(body), {
      status: 200,
      headers: { 'content-type': 'application/json' },
    }),
  )
}

function answer(): void {
  fetchMock.mockImplementation(async (input: unknown) => {
    const req = input as Request
    const url = new URL(req.url, 'http://t')
    const path = url.pathname.replace(/^\/api/, '')
    if (path === '/tasks/digest') {
      digestCalls.push(url.searchParams)
      return json(answerDigest)
    }
    if (path === '/tags' && url.searchParams.get('kind') === 'client') {
      return json([{ id: CLIENT, name: 'Cylock', kind: 'client' }])
    }
    return json([])
  })
}

let root: Root | null = null
let host: HTMLDivElement | null = null

async function render(): Promise<HTMLDivElement> {
  const el = document.createElement('div')
  host = el
  document.body.appendChild(el)
  await act(async () => {
    root = createRoot(el)
    root.render(
      <MemoryRouter>
        <DiaryRoute />
      </MemoryRouter>,
    )
  })
  await act(async () => {
    await new Promise((r) => setTimeout(r, 0))
  })
  return el
}

beforeEach(async () => {
  setSession({ token: 't', workspaceId: '00000000-0000-4000-8000-000000000001' })
  digestCalls.length = 0
  answerDigest = DIGEST
  answer()
  await i18n.changeLanguage('en')
})

afterEach(() => {
  act(() => root?.unmount())
  root = null
  host?.remove()
  host = null
  clearSession()
  fetchMock.mockReset()
})

describe('digestWindow', () => {
  it('is the picked local days, end exclusive at the next local midnight', () => {
    const w = digestWindow('2026-11-09', '2026-11-15')!
    expect(new Date(w.start_from).getTime()).toBe(new Date(2026, 10, 9).getTime())
    expect(new Date(w.start_to).getTime()).toBe(new Date(2026, 10, 16).getTime())
    // An offset is always present: the server refuses a naive instant.
    expect(w.start_from).toMatch(/[+-]\d\d:\d\d$/)
  })

  it('writes each end with its own local offset, so a quarter across DST is 92 days', () => {
    // The server counts wall-clock days from each end's offset. In UTC the
    // Q4 of Europe/Rome was 92 days and one hour, and it was refused.
    // stubEnv assigns process.env.TZ, and Node re-reads the zone on that.
    vi.stubEnv('TZ', 'Europe/Rome')
    try {
      const w = digestWindow('2026-10-01', '2026-12-31')!
      expect(w).toEqual({
        start_from: '2026-10-01T00:00:00+02:00',
        start_to: '2027-01-01T00:00:00+01:00',
      })
    } finally {
      vi.unstubAllEnvs()
    }
  })

  it.each([
    // A change of clock at the midnight that ends the range: the end is the
    // close of the last day, on that day's clock, not 01:00 on the next.
    ['America/Santiago', '2025-06-07', '2025-09-06', '2025-06-07T00:00:00-04:00', '2025-09-07T00:00:00-04:00'],
    // The same at the start: midnight does not exist, the day begins at 01:00.
    ['America/Santiago', '2025-09-07', '2025-09-08', '2025-09-07T01:00:00-03:00', '2025-09-09T00:00:00-03:00'],
    // Offsets that are not whole hours keep their minutes.
    ['Asia/Kathmandu', '2026-01-01', '2026-01-01', '2026-01-01T00:00:00+05:45', '2026-01-02T00:00:00+05:45'],
    ['Pacific/Chatham', '2026-07-01', '2026-07-01', '2026-07-01T00:00:00+12:45', '2026-07-02T00:00:00+12:45'],
    ['America/St_Johns', '2026-01-01', '2026-01-01', '2026-01-01T00:00:00-03:30', '2026-01-02T00:00:00-03:30'],
    ['UTC', '2026-01-01', '2026-01-01', '2026-01-01T00:00:00+00:00', '2026-01-02T00:00:00+00:00'],
  ])('in %s, %s..%s is %s .. %s', (tz, from, to, start, end) => {
    vi.stubEnv('TZ', tz)
    try {
      const w = digestWindow(from, to)!
      expect(w).toEqual({ start_from: start, start_to: end })
      // Each string names the instant the browser means by that midnight.
      const [y, m, d] = to.split('-').map(Number)
      expect(Date.parse(w.start_to)).toBe(new Date(y, m - 1, d + 1).getTime())
    } finally {
      vi.unstubAllEnvs()
    }
  })

  it('refuses an inverted or unparseable range', () => {
    expect(digestWindow('2026-11-15', '2026-11-09')).toBeNull()
    expect(digestWindow('', '2026-11-09')).toBeNull()
  })
})

describe('digestMarkdown', () => {
  it('carries every event with its text, and the description', () => {
    const md = digestMarkdown(i18n.t.bind(i18n), DIGEST, {
      scope: 'Cylock',
      from: '2026-11-09',
      to: '2026-11-15',
    })
    expect(md).toContain('## Ingest schema [00000000]')
    expect(md).toContain('> agreed with the client')
    expect(md).toContain('State: todo → in_progress @angelo')
    expect(md).toContain('Time 1h30 @angelo')
    expect(md).toContain('  migrated the ingest\n  and ran the backfill')
    expect(md).toContain('Comment @claude (via agent)')
    expect(md).toContain('  schema v2 signed off')
  })
})

describe('DiaryRoute', () => {
  it('asks for the current week with an offset, then narrows to the picked client', async () => {
    const host = await render()
    expect(digestCalls.length).toBeGreaterThan(0)
    const first = digestCalls[digestCalls.length - 1]
    expect(first.get('start_from')).toMatch(/[+-]\d\d:\d\d$/)
    expect(first.get('client_tag_id')).toBeNull()
    const days =
      (new Date(first.get('start_to')!).getTime() - new Date(first.get('start_from')!).getTime()) /
      86_400_000
    expect(Math.round(days)).toBe(7)

    const select = host.querySelector('select') as HTMLSelectElement
    await act(async () => {
      select.value = CLIENT
      select.dispatchEvent(new Event('change', { bubbles: true }))
    })
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0))
    })
    expect(digestCalls[digestCalls.length - 1].get('client_tag_id')).toBe(CLIENT)
  })

  it('shows every event with its text', async () => {
    const host = await render()
    const text = host.textContent ?? ''
    expect(text).toContain('Ingest schema')
    expect(text).toContain('State: todo → in_progress')
    expect(text).toContain('migrated the ingest')
    expect(text).toContain('schema v2 signed off')
    expect(host.querySelectorAll('.diary__event')).toHaveLength(3)
  })

  it('says what is withheld and when nothing happened', async () => {
    answerDigest = { ...DIGEST, withheld: ['comments:read'], tasks: [], seconds: 0, billable_seconds: 0 }
    const host = await render()
    const text = host.textContent ?? ''
    expect(text).toContain('Not readable by this credential: comments:read')
    expect(text).toContain('Nothing happened on these tasks in this period.')
  })
})
