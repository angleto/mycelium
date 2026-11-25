import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { GardenReviewRoute } from './GardenReviewRoute'
import { clearSession, setSession } from '../auth/session'
import i18n from '../i18n'

// What the proposals page promises (ADR-0075), against a double of the API
// that answers the listings and records every write:
// - four tabs with their counts, not one long page;
// - every kind of proposal in one list, filterable by kind;
// - a link is decided on its row with the kind the person chose, a fact with
//   words, and a synthesis is opened to be read before it is decided;
// - a decision taken here can be undone on the spot (reopen);
// - a decided proposal offers Reopen, or says it cannot be reopened;
// - suggested work offers only what the web can do, and counts the rest.

const fetchMock = vi.hoisted(() => {
  const fn = vi.fn<(...args: unknown[]) => Promise<Response>>()
  globalThis.fetch = fn as unknown as typeof globalThis.fetch
  return fn
})

const id = (n: number) => `00000000-0000-4000-8000-0000000000${String(n).padStart(2, '0')}`
const SYN = id(1)
const LINK = id(2)
const FACT = id(3)

const base = {
  version: 1,
  created_at: '2026-11-24T10:00:00Z',
  decided_at: null,
  proposer: 'mistral-small',
  reopenable: false,
  link_kinds: [],
}
const PENDING = [
  {
    ...base,
    kind: 'synthesis',
    id: SYN,
    state: 'pending',
    subjects: [{ note_id: id(10), title: 'Diario' }],
    title: 'Distillation · Diario',
    preview: 'Una lezione.',
    humus_kind: 'distillation',
  },
  {
    ...base,
    kind: 'link',
    id: LINK,
    version: null,
    state: 'pending',
    subjects: [
      { note_id: id(11), title: 'Alfa' },
      { note_id: id(12), title: 'Beta' },
    ],
    proposer: 'cooccurrence',
    reason: 'often read together',
    link_kinds: ['related', 'contradicts', 'hypha_of', 'supersedes'],
  },
  {
    ...base,
    kind: 'fact',
    id: FACT,
    state: 'pending',
    subjects: [{ note_id: id(13), title: 'Riunione' }],
    fact_subject: 'Anna',
    fact_predicate: 'member_of',
    fact_object: 'Acme',
  },
]
const DECIDED = [
  { ...PENDING[1], state: 'accepted', reopenable: true, decided_at: '2026-11-24T11:00:00Z' },
  { ...PENDING[2], state: 'declined', reopenable: false, decided_at: '2026-11-24T11:00:00Z' },
]

const writes: { path: string; body: Record<string, unknown> }[] = []

function json(body: unknown, status = 200): Promise<Response> {
  return Promise.resolve(
    new Response(JSON.stringify(body), {
      status,
      headers: { 'content-type': 'application/json' },
    }),
  )
}

function answer(): void {
  fetchMock.mockImplementation(async (input: unknown) => {
    const req = input as Request
    const url = new URL(req.url, 'http://t')
    const path = url.pathname.replace(/^\/api/, '')
    if (req.method === 'GET' && path === '/garden/proposals') {
      const listing = url.searchParams.get('listing')
      return json({ items: listing === 'decided' ? DECIDED : PENDING, next_cursor: null })
    }
    if (req.method === 'GET' && path === '/garden/delegation/decisions') return json([])
    if (req.method === 'GET' && path === '/garden/candidates') {
      return json({
        nodes: [
          {
            kind: 'pattern',
            note_ids: [id(20), id(21)],
            title: 'Pattern · 2 notes',
            reason: 'a cluster',
            score: 1,
            preview: '',
          },
        ],
        edges: [
          {
            op: 'prune',
            src_note_id: id(22),
            dst_note_id: id(23),
            link_kind: 'related',
            src_title: 'x',
            dst_title: 'y',
            reason: 'decayed',
            score: 1,
          },
        ],
      })
    }
    if (req.method === 'POST') {
      const body = (await req.json()) as Record<string, unknown>
      writes.push({ path, body })
      if (path === '/garden/synthesize/pattern') {
        return json({ pattern_note_id: id(30), model_id: 'm', created: true })
      }
      return json({ kind: body.kind, id: body.id, done: true })
    }
    return json({})
  })
}

let host: HTMLDivElement
let root: Root

async function settle(): Promise<void> {
  for (let i = 0; i < 3; i++) {
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0))
    })
  }
}

async function mount(at = '/garden/review'): Promise<void> {
  await act(async () => {
    root.render(
      <MemoryRouter initialEntries={[at]}>
        <Routes>
          <Route path="/garden/review" element={<GardenReviewRoute />} />
          <Route path="/garden/review/:noteId" element={<p data-testid="reading">reading</p>} />
        </Routes>
      </MemoryRouter>,
    )
  })
  await settle()
}

const byRole = (role: string, name: string) =>
  [...host.querySelectorAll<HTMLElement>(`[role="${role}"], ${role}`)].find((e) =>
    e.textContent?.trim().startsWith(name),
  )!
const buttonNamed = (name: string, scope: ParentNode = host) =>
  [...scope.querySelectorAll<HTMLElement>('button')].find((b) => b.textContent?.trim() === name)!
const rowOf = (text: string) =>
  [...host.querySelectorAll<HTMLElement>('li.gprop')].find((li) => li.textContent?.includes(text))!

async function click(el: Element): Promise<void> {
  await act(async () => {
    ;(el as HTMLElement).click()
  })
  await settle()
}

beforeEach(async () => {
  await i18n.changeLanguage('it')
  setSession({ token: 't', workspaceId: '00000000-0000-4000-8000-0000000000aa' })
  writes.length = 0
  answer()
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
})

afterEach(() => {
  act(() => root.unmount())
  host.remove()
  clearSession()
  fetchMock.mockReset()
})

describe('GardenReviewRoute', () => {
  it('shows four tabs with counts and every kind of proposal in one list', async () => {
    await mount()
    const tabs = [...host.querySelectorAll('[role="tab"]')].map((x) => x.textContent)
    expect(tabs).toEqual([
      `${i18n.t('proposals.tab.pending')} (3)`,
      `${i18n.t('proposals.tab.decided')} (2)`,
      `${i18n.t('proposals.tab.agents')} (0)`,
      `${i18n.t('proposals.tab.work')} (1)`,
    ])
    expect(host.querySelectorAll('li.gprop')).toHaveLength(3)
    await click(byRole('button', i18n.t('proposals.filterKind.fact')))
    expect(host.querySelectorAll('li.gprop')).toHaveLength(1)
    expect(host.textContent).toContain('Anna')
  })

  it('links two notes with the chosen kind, and undoes it on the spot', async () => {
    await mount()
    const row = rowOf('Alfa')
    const select = row.querySelector('select')!
    await act(async () => {
      select.value = 'contradicts'
      select.dispatchEvent(new Event('change', { bubbles: true }))
    })
    await click(buttonNamed(i18n.t('proposals.link.accept'), row))
    expect(writes[0]).toEqual({
      path: '/garden/proposals/decide',
      body: {
        kind: 'link',
        id: LINK,
        verdict: 'accept',
        expected_version: null,
        link_kind: 'contradicts',
      },
    })
    expect(rowOf('Alfa').textContent).toContain(i18n.t('proposals.done.link.accept'))
    await click(buttonNamed(i18n.t('proposals.undo'), rowOf('Alfa')))
    expect(writes[1]).toEqual({ path: '/garden/proposals/reopen', body: { kind: 'link', id: LINK } })
  })

  it('declines a fact in words and sends the synthesis to be read', async () => {
    await mount()
    await click(buttonNamed(i18n.t('proposals.fact.decline'), rowOf('Anna')))
    expect(writes[0].body).toMatchObject({ kind: 'fact', id: FACT, verdict: 'decline', expected_version: 1 })
    const read = [...rowOf('Diario').querySelectorAll('a')].find(
      (a) => a.textContent === i18n.t('gardenReview.readAndDecide'),
    )
    expect(read?.getAttribute('href')).toBe(`/garden/review/${SYN}`)
  })

  it('offers Reopen on a decided proposal, or says it cannot be reopened', async () => {
    await mount('/garden/review?tab=decided')
    expect(buttonNamed(i18n.t('proposals.reopen'), rowOf('Alfa'))).toBeTruthy()
    expect(rowOf('Anna').textContent).toContain(i18n.t('proposals.notReopenable'))
    expect(buttonNamed(i18n.t('proposals.reopen'), rowOf('Anna'))).toBeUndefined()
  })

  it('offers only the suggested work the web can do, and counts the rest', async () => {
    await mount('/garden/review?tab=work')
    expect(host.querySelectorAll('li.gprop')).toHaveLength(1)
    expect(host.textContent).toContain(i18n.t('proposals.work.notHere', { count: 1 }))
    await click(buttonNamed(i18n.t('proposals.work.pattern.action')))
    expect(writes[0]).toEqual({
      path: '/garden/synthesize/pattern',
      body: { source_note_ids: [id(20), id(21)] },
    })
    expect(host.querySelector('[data-testid="reading"]')).not.toBeNull()
  })
})
