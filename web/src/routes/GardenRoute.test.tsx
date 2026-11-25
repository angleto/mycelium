import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { GardenRoute } from './GardenRoute'
import { clearSession, setSession } from '../auth/session'
import i18n from '../i18n'

// What the garden's board and its "to decide" filter promise (task
// e584ed61), against a double of the API that answers the list the page
// reads and records every write:
// - any card but a transplanted one goes into growing or mature, from any
//   column, by drag or by its buttons, and the move is the maturity
//   endpoint the list already uses;
// - the seed and dormant columns take no card, say why, and no drop on them
//   reaches the server, not even one the browser was never asked about;
// - a drop on the card's own column writes nothing;
// - a transplanted note sits with the dormant ones and cannot be moved;
// - the "work closed" chip is the server's flag, and "To decide" shows the
//   flagged notes and nothing else, on the board and in the list;
// - a stage the view does not know can be moved to growing or mature;
// - after a move or an archive the keyboard lands on the moved card or a
//   neighbour, not on the page body;
// - the list or board choice is kept per workspace.

const fetchMock = vi.hoisted(() => {
  const fn = vi.fn<(...args: unknown[]) => Promise<Response>>()
  globalThis.fetch = fn as unknown as typeof globalThis.fetch
  return fn
})

type Row = {
  id: string
  title: string
  maturity: string
  work_closed?: boolean
  promoted_at?: string | null
  is_archived?: boolean
}

const id = (n: number) => `00000000-0000-4000-8000-0000000000${String(n).padStart(2, '0')}`
const ROWS: Row[] = [
  { id: id(1), title: 'Seed, work closed', maturity: 'seed', work_closed: true },
  { id: id(2), title: 'Growing, work open', maturity: 'growing' },
  { id: id(3), title: 'Mature', maturity: 'mature' },
  { id: id(4), title: 'Dormant', maturity: 'dormant' },
  { id: id(5), title: 'Transplanted', maturity: 'growing', promoted_at: '2026-11-02T00:00:00Z' },
  { id: id(6), title: 'Growing, work closed', maturity: 'growing', work_closed: true },
]

const writes: { path: string; body: unknown }[] = []
// What the server holds: a write changes it, so a moved card is drawn again
// under its new column, as in the app.
let rows: Row[] = []
// The write verbs the server refuses in this test (409).
let refuse: string[] = []

function row(r: Row) {
  return {
    kind: 'text',
    status: 'ready',
    protected: false,
    is_archived: false,
    version: 7,
    tags: [],
    derived_task_ids: [],
    linked_task_count: 0,
    work_closed: false,
    promoted_at: null,
    created_at: '2026-11-01T00:00:00Z',
    updated_at: '2026-11-01T00:00:00Z',
    ...r,
  }
}

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
    const path = new URL(req.url, 'http://t').pathname.replace(/^\/api/, '')
    if (req.method === 'GET' && path === '/notes')
      return json({ items: rows.map(row), next_cursor: null, truncated: false })
    if (req.method === 'GET' && path === '/tasks') return json([])
    if (path === '/garden/candidates') return json({ nodes: [], edges: [] })
    if (path === '/garden/review/pending') return json([])
    // The note view mounts the connections panel (ADR-0077), which reads
    // the item's connections and the workflow states for its task picker.
    if (req.method === 'GET' && path === '/connections') return json({ items: [], choices: [] })
    if (req.method === 'GET' && path === '/workflows') return json([])
    const links = /^\/notes\/([^/]+)\/links$/.exec(path)
    if (req.method === 'GET' && links) {
      const r = rows.find((x) => x.id === links[1])!
      return json({ note: { ...row(r), transcript: '' }, outgoing: [], incoming: [], task_links: [] })
    }
    if (req.method === 'POST' && refuse.some((v) => path.endsWith(`/${v}`))) {
      return new Response(JSON.stringify({ code: 'conflict', detail: 'stale' }), {
        status: 409,
        headers: { 'content-type': 'application/json' },
      })
    }
    if (req.method === 'POST' && path.endsWith('/distill')) {
      writes.push({ path, body: null })
      return json({ distilled_note_id: path.split('/')[2], review_state: 'approved' })
    }
    if (req.method === 'POST') {
      const body = (await req.json()) as { maturity?: string }
      writes.push({ path, body })
      const [, , noteId, verb] = path.split('/')
      rows = rows.map((r) =>
        r.id !== noteId
          ? r
          : verb === 'maturity'
            ? { ...r, maturity: body.maturity ?? r.maturity }
            : verb === 'archive'
              ? { ...r, is_archived: true }
              : r,
      )
      return json({ id: path.split('/')[2], version: 8 })
    }
    return json({})
  })
}

let host: HTMLDivElement
let root: Root

async function settle(): Promise<void> {
  await act(async () => {
    await new Promise((r) => setTimeout(r, 0))
  })
}

async function mount(): Promise<void> {
  await act(async () => {
    root.render(
      <MemoryRouter>
        <GardenRoute />
      </MemoryRouter>,
    )
  })
  await settle()
  await settle()
}

async function click(el: Element): Promise<void> {
  await act(async () => {
    ;(el as HTMLElement).click()
  })
  await settle()
}

function byText(selector: string, text: string): HTMLElement {
  const el = [...host.querySelectorAll<HTMLElement>(selector)].find(
    (e) => e.textContent?.trim() === text,
  )
  if (!el) throw new Error(`no ${selector} reading "${text}"`)
  return el
}

async function showBoard(): Promise<void> {
  await click(byText('.viewtabs__tab', i18n.t('garden.view.kanban')))
}

const column = (c: string) => host.querySelector<HTMLElement>(`[data-column="${c}"]`)!
const card = (n: number) => host.querySelector<HTMLElement>(`[data-note-id="${id(n)}"]`)!
const titlesIn = (c: string) =>
  [...column(c).querySelectorAll('.kanban__card .kanban__title')].map((e) => e.textContent)

// jsdom has no DataTransfer; React reads the native event's, so a minimal
// one carried on a plain Event is what the handlers see.
function dataTransfer(): DataTransfer {
  const store = new Map<string, string>()
  return {
    dropEffect: 'none',
    effectAllowed: 'all',
    setData: (k: string, v: string) => void store.set(k, v),
    getData: (k: string) => store.get(k) ?? '',
  } as unknown as DataTransfer
}

function fire(el: Element, type: string, dt: DataTransfer): Event {
  const ev = new Event(type, { bubbles: true, cancelable: true })
  Object.defineProperty(ev, 'dataTransfer', { value: dt })
  el.dispatchEvent(ev)
  return ev
}

/** Drag card ``n`` onto column ``c``; returns whether the column accepted
 * the dragover (the browser fires the drop only when it did). */
async function drag(n: number, c: string): Promise<boolean> {
  const dt = dataTransfer()
  let accepted = false
  await act(async () => {
    fire(card(n), 'dragstart', dt)
  })
  await act(async () => {
    accepted = fire(column(c), 'dragover', dt).defaultPrevented
  })
  await act(async () => {
    fire(column(c), 'drop', dt)
  })
  await settle()
  return accepted
}

beforeEach(() => {
  setSession({ token: 't', workspaceId: '00000000-0000-4000-8000-000000000001' })
  localStorage.clear()
  writes.length = 0
  rows = ROWS.map((r) => ({ ...r }))
  refuse = []
  answer()
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

describe('GardenRoute board', () => {
  it('draws one column per stage, the transplanted note with the dormant ones', async () => {
    await mount()
    await showBoard()
    expect(titlesIn('seed')).toEqual(['Seed, work closed'])
    expect(titlesIn('growing')).toEqual(['Growing, work open', 'Growing, work closed'])
    expect(titlesIn('mature')).toEqual(['Mature'])
    expect(titlesIn('dormant')).toEqual(['Dormant', 'Transplanted'])
    // Every card but the transplanted one can be picked up.
    const draggable = (n: number) => card(n).getAttribute('draggable')
    expect([1, 2, 3, 4, 5, 6].map(draggable)).toEqual([
      'true',
      'true',
      'true',
      'true',
      'false',
      'true',
    ])
  })

  it('a drop into growing or mature is written, from any column', async () => {
    await mount()
    await showBoard()
    expect(await drag(2, 'mature')).toBe(true)
    expect(await drag(3, 'growing')).toBe(true)
    // seed -> mature: the case the board exists for (a seed whose work is
    // closed, marked mature in one move).
    expect(await drag(1, 'mature')).toBe(true)
    expect(await drag(1, 'growing')).toBe(true)
    expect(await drag(4, 'growing')).toBe(true)
    expect(await drag(4, 'mature')).toBe(true)
    expect(writes).toEqual([
      { path: `/notes/${id(2)}/maturity`, body: { maturity: 'mature' } },
      { path: `/notes/${id(3)}/maturity`, body: { maturity: 'growing' } },
      { path: `/notes/${id(1)}/maturity`, body: { maturity: 'mature' } },
      { path: `/notes/${id(1)}/maturity`, body: { maturity: 'growing' } },
      { path: `/notes/${id(4)}/maturity`, body: { maturity: 'growing' } },
      { path: `/notes/${id(4)}/maturity`, body: { maturity: 'mature' } },
    ])
  })

  it('the seed and dormant columns refuse every drop and say why', async () => {
    await mount()
    await showBoard()
    const why = i18n.t('garden.kanban.timeManaged')
    for (const c of ['seed', 'dormant']) {
      expect(column(c).querySelector('.kanban__hint')?.textContent).toBe(why)
      for (const n of [1, 2, 3, 4, 6]) expect(await drag(n, c)).toBe(false)
    }
    // The columns that take cards carry no such line.
    expect(column('growing').querySelector('.kanban__hint')).toBeNull()
    expect(column('mature').querySelector('.kanban__hint')).toBeNull()
    expect(writes).toEqual([])
  })

  it('a transplanted card goes nowhere, and a drop on its own column is no move', async () => {
    await mount()
    await showBoard()
    // The drop handler holds the rule even with no dragover before it.
    expect(await drag(5, 'growing')).toBe(false)
    expect(await drag(5, 'mature')).toBe(false)
    expect(await drag(2, 'growing')).toBe(false)
    expect(await drag(6, 'growing')).toBe(false)
    expect(await drag(3, 'mature')).toBe(false)
    expect(writes).toEqual([])
  })

  it('moves a card without dragging, to each column a drag could reach', async () => {
    await mount()
    await showBoard()
    const moveTo = (stage: string) =>
      `${stage === 'mature' ? '🌳' : '🌿'} ${i18n.t('garden.kanban.moveTo', {
        stage: i18n.t(`garden.maturity.${stage}`),
      })}`
    const moves = (n: number) =>
      [...card(n).querySelectorAll('button')]
        .map((b) => b.textContent?.trim())
        .filter((b) => b === moveTo('growing') || b === moveTo('mature'))
    expect(moves(1)).toEqual([moveTo('growing'), moveTo('mature')])
    expect(moves(2)).toEqual([moveTo('mature')])
    expect(moves(3)).toEqual([moveTo('growing')])
    expect(moves(4)).toEqual([moveTo('growing'), moveTo('mature')])
    expect(moves(5)).toEqual([])
    await click(byText(`[data-note-id="${id(1)}"] button`, moveTo('mature')))
    expect(writes).toEqual([{ path: `/notes/${id(1)}/maturity`, body: { maturity: 'mature' } }])
  })

  it('shows the server flag as a chip, and archives a flagged note at the version read', async () => {
    await mount()
    await showBoard()
    const chip = i18n.t('garden.workClosed.chip')
    const flagged = [1, 2, 3, 4, 5, 6].filter((n) =>
      [...card(n).querySelectorAll('.plant__chip--closed')].some((e) => e.textContent === chip),
    )
    expect(flagged).toEqual([1, 6])
    await click(byText(`[data-note-id="${id(1)}"] button`, i18n.t('garden.archive')))
    expect(writes).toEqual([{ path: `/notes/${id(1)}/archive`, body: { expected_version: 7 } }])
  })
})

describe('GardenRoute "to decide" filter', () => {
  const toggle = () =>
    [...host.querySelectorAll<HTMLElement>('[role="switch"]')].find((e) =>
      e.textContent?.includes(i18n.t('garden.workClosed.filter')),
    )!

  it('keeps exactly the flagged notes on the board', async () => {
    await mount()
    await showBoard()
    expect(toggle().textContent).toContain('2')
    await click(toggle())
    expect(toggle().getAttribute('aria-checked')).toBe('true')
    const all = [...host.querySelectorAll('.kanban__card .kanban__title')].map((e) => e.textContent)
    expect(all).toEqual(['Seed, work closed', 'Growing, work closed'])
  })

  it('keeps exactly the flagged notes in the list, tab by tab', async () => {
    await mount()
    await click(toggle())
    const listed = () => [...host.querySelectorAll('.plant .plant__title')].map((e) => e.textContent)
    expect(listed()).toEqual(['Seed, work closed'])
    await click(byText('.garden__tab-label', i18n.t('garden.tab.garden')).closest('button')!)
    expect(listed()).toEqual(['Growing, work closed'])
    await click(byText('.garden__tab-label', i18n.t('garden.tab.cemetery')).closest('button')!)
    expect(listed()).toEqual([])
    expect(host.textContent).toContain(i18n.t('garden.workClosed.empty'))
  })
})

describe('GardenRoute board, edges', () => {
  const moveTo = (stage: string) =>
    `${stage === 'mature' ? '🌳' : '🌿'} ${i18n.t('garden.kanban.moveTo', {
      stage: i18n.t(`garden.maturity.${stage}`),
    })}`

  it('a stage the view does not know is drawn as growing and can be moved to growing or mature', async () => {
    rows.push({ id: id(7), title: 'Unknown stage', maturity: 'sprouting' })
    await mount()
    await showBoard()
    expect(titlesIn('growing')).toContain('Unknown stage')
    const moves = [...card(7).querySelectorAll('button')]
      .map((b) => b.textContent?.trim())
      .filter((b) => b === moveTo('growing') || b === moveTo('mature'))
    expect(moves).toEqual([moveTo('growing'), moveTo('mature')])
    expect(card(7).getAttribute('draggable')).toBe('true')
    expect(await drag(7, 'growing')).toBe(true)
    expect(writes).toEqual([{ path: `/notes/${id(7)}/maturity`, body: { maturity: 'growing' } }])
  })

  it('a transplanted note whose work is closed gets no action at all', async () => {
    rows.push({
      id: id(8),
      title: 'Transplanted, work closed',
      maturity: 'mature',
      promoted_at: '2026-11-02T00:00:00Z',
      work_closed: true,
    })
    await mount()
    await showBoard()
    expect(titlesIn('dormant')).toContain('Transplanted, work closed')
    const buttons = [...card(8).querySelectorAll('button')].map((b) => b.textContent?.trim())
    // Only its title, which opens the detail.
    expect(buttons).toEqual(['Transplanted, work closed'])
    expect(card(8).getAttribute('draggable')).toBe('false')
    // And in the list, where the row has its own archive control.
    await click(byText('.viewtabs__tab', i18n.t('garden.view.list')))
    await click(byText('.garden__tab-label', i18n.t('garden.tab.cemetery')).closest('button')!)
    const row8 = host.querySelector(`.plant[data-note-id="${id(8)}"]`)!
    expect(row8.querySelector(`[aria-label="${i18n.t('garden.archive')}"]`)).toBeNull()
  })

  it('after a move the keyboard is on the moved card, in its new column', async () => {
    await mount()
    await showBoard()
    const button = byText(`[data-note-id="${id(1)}"] button`, moveTo('mature'))
    button.focus()
    await click(button)
    await settle()
    expect(titlesIn('mature')).toContain('Seed, work closed')
    const title = column('mature').querySelector(`[data-note-id="${id(1)}"] .garden__card-title`)
    expect(document.activeElement).toBe(title)
  })

  it('after an archive the keyboard is on a neighbour of the archived card', async () => {
    await mount()
    await showBoard()
    const button = byText(`[data-note-id="${id(6)}"] button`, i18n.t('garden.archive'))
    button.focus()
    await click(button)
    await settle()
    expect(host.querySelector(`[data-note-id="${id(6)}"]`)).toBeNull()
    // Its only neighbour in the growing column is the card above it.
    expect(document.activeElement).toBe(card(2).querySelector('.garden__card-title'))
  })

  it('after archiving the last card of a column the keyboard is on the page heading', async () => {
    await mount()
    await showBoard()
    const button = byText(`[data-note-id="${id(1)}"] button`, i18n.t('garden.archive'))
    button.focus()
    await click(button)
    await settle()
    expect(document.activeElement).toBe(host.querySelector('h1'))
  })

  it('keeps the list or board choice per workspace', async () => {
    await mount()
    await showBoard()
    act(() => root.unmount())
    root = createRoot(host)
    await mount()
    expect(byText('.viewtabs__tab', i18n.t('garden.view.kanban')).getAttribute('aria-selected')).toBe('true')
    expect(column('seed')).not.toBeNull()
    act(() => root.unmount())
    setSession({ token: 't', workspaceId: '00000000-0000-4000-8000-000000000002' })
    root = createRoot(host)
    await mount()
    expect(byText('.viewtabs__tab', i18n.t('garden.view.list')).getAttribute('aria-selected')).toBe('true')
    expect(host.querySelector('[data-column]')).toBeNull()
  })
})

describe('GardenRoute focus after a refused write', () => {
  it.each(['archive', 'maturity'])(
    'a refused %s leaves no focus target for the next reload to spend',
    async (verb) => {
      refuse = [verb]
      await mount()
      await showBoard()
      const label =
        verb === 'archive'
          ? i18n.t('garden.archive')
          : `🌳 ${i18n.t('garden.kanban.moveTo', { stage: i18n.t('garden.maturity.mature') })}`
      await click(byText(`[data-note-id="${id(6)}"] button`, label))
      expect(host.querySelector('.err')?.textContent).toBeTruthy()
      // An unrelated reload: a distillation from the list, which re-reads
      // the notes and opens the plant detail without setting a target.
      refuse = []
      await click(byText('.viewtabs__tab', i18n.t('garden.view.list')))
      await click(byText('.garden__tab-label', i18n.t('garden.tab.cemetery')).closest('button')!)
      const distill = host.querySelector<HTMLElement>(
        `.plant[data-note-id="${id(4)}"] [aria-label="${i18n.t('garden.distill')}"]`,
      )!
      ;(document.activeElement as HTMLElement | null)?.blur()
      await click(distill)
      await settle()
      expect(writes.some((w) => w.path.endsWith('/distill'))).toBe(true)
      // The stale target (a neighbour of card 6, not on the cemetery tab)
      // would have fallen back to the heading.
      expect(document.activeElement).not.toBe(host.querySelector('h1'))
    },
  )
})
