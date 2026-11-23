import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { PinnedNotes } from './PinnedNotes'
import { RecentNotes } from './RecentNotes'
import { PinButton } from './PinButton'
import { notePins, PIN_MOVING_REQUESTS, pinKindsMovedBy, resetPinsForTest } from '../api/pins'
import { api, workspaceHeader } from '../api/client'
import { clearSession, setSession } from '../auth/session'
import '../i18n'

// What the pinned notes promise (migration 0033), against a double of the
// API that keeps its own set of pins, so a press is answered the way the
// server answers it:
// - nothing at all is drawn while nothing is pinned;
// - pins come back newest first, an archived note like any other;
// - the pin control on a row of this list takes the note off it, and says
//   so to the server;
// - the recent notes leave the pinned ones out and give them back on unpin;
// - note pins and task pins are separate stores: pinning a note is not
//   pinning a task with the same id;
// - a note put in the bin from ANY screen leaves the pinned list, and comes
//   back when restored (the server drops it; the store must re-read);
// - a refused press is undone on screen and says why, even when the read
//   that follows fails too.

// Installed before the imports run: openapi-fetch reads `globalThis.fetch`
// when the client module is created.
const fetchMock = vi.hoisted(() => {
  const fn = vi.fn<(...args: unknown[]) => Promise<Response>>()
  globalThis.fetch = fn as unknown as typeof globalThis.fetch
  return fn
})

type N = { id: string; title: string; is_archived: boolean }
const NOTES: Record<string, N> = {
  a: { id: '00000000-0000-4000-8000-00000000000a', title: 'Aperta', is_archived: false },
  b: { id: '00000000-0000-4000-8000-00000000000b', title: 'Archiviata', is_archived: true },
}

let pins: string[] = []
let binned: string[] = []
// The status each kind of request answers with; 200 unless a test says so.
let failWrite: { status: number; body: unknown } | null = null
let failRead = false
// While set, a pinned-list read takes its answer when it arrives and only
// sends it once the gate opens: a read overtaken by a change on the server.
let gate: Promise<void> | null = null
const calls: string[] = []

function row(n: N) {
  return {
    ...n,
    kind: 'text',
    status: 'ready',
    protected: false,
    version: 1,
    tags: [],
    derived_task_ids: [],
    linked_task_count: 0,
    created_at: '2026-11-01T00:00:00Z',
    updated_at: '2026-11-01T00:00:00Z',
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
    const url = new URL(req.url, 'http://t')
    const path = url.pathname.replace(/^\/api/, '')
    calls.push(`${req.method} ${path}${url.search}`)
    const find = (id: string) => Object.values(NOTES).find((n) => n.id === id)!
    if (req.method === 'GET' && path === '/notes') {
      if (failRead) return failure(500, { code: 'internal', detail: 'down' })
      // The pinned list, or the recent one: every note, most recent first.
      // A note in the bin is in neither, as on the server.
      const items = (
        url.searchParams.get('pinned') === 'true'
          ? pins.map((id) => find(id))
          : Object.values(NOTES)
      )
        .filter((n) => !binned.includes(n.id))
        .map(row)
      if (gate) await gate
      return json({ items, next_cursor: null, truncated: false })
    }
    if (req.method === 'GET' && path === '/tasks') return json([])
    const life = /^\/notes\/([^/]+)\/(delete|restore|erase)$/.exec(path)
    if (life) {
      if (life[2] === 'restore') binned = binned.filter((x) => x !== life[1])
      else binned = [...binned, life[1]]
      return json({ version: 2 })
    }
    if (path === '/workspaces/me/trash/empty') return json({ purged: 0 })
    if (path === '/garden/review/restore-source') {
      // The atom goes back to review, which drops its pins on the server.
      const { atom_note_id } = (await req.json()) as { atom_note_id: string }
      pins = pins.filter((x) => x !== atom_note_id)
      return json({ restored_source_ids: [], atom_retired: true })
    }
    if (failWrite) return failure(failWrite.status, failWrite.body)
    const { note_id } = (await req.json()) as { note_id: string }
    if (path === '/notes/pin') pins = [note_id, ...pins.filter((x) => x !== note_id)]
    if (path === '/notes/unpin') pins = pins.filter((x) => x !== note_id)
    return json({ note_id, pinned: path === '/notes/pin' })
  })
}

function failure(status: number, body: unknown): Promise<Response> {
  return Promise.resolve(
    new Response(JSON.stringify(body), {
      status,
      headers: { 'content-type': 'application/json' },
    }),
  )
}

let host: HTMLDivElement
let root: Root

async function mount(extra: React.ReactNode = null): Promise<void> {
  await act(async () => {
    root.render(
      <MemoryRouter>
        <PinnedNotes notes={[]} />
        {extra}
      </MemoryRouter>,
    )
  })
  // Let the stores' first reads land.
  await act(async () => {
    await new Promise((r) => setTimeout(r, 0))
  })
}

function titles(): string[] {
  return [...host.querySelectorAll('.pinnedwidget .recentrow__title')].map(
    (e) => e.textContent ?? '',
  )
}

beforeEach(() => {
  setSession({ token: 't', workspaceId: '00000000-0000-4000-8000-000000000001' })
  resetPinsForTest()
  pins = []
  binned = []
  failWrite = null
  failRead = false
  gate = null
  calls.length = 0
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

describe('PinnedNotes', () => {
  it('draws nothing at all while nothing is pinned', async () => {
    await mount()
    expect(host.querySelector('.pinnedwidget')).toBeNull()
    expect(calls).toContain('GET /notes?pinned=true&limit=200')
  })

  it('lists the pins newest first, an archived note like any other', async () => {
    pins = [NOTES.b.id, NOTES.a.id]
    await mount()
    expect(titles()).toEqual(['Archiviata', 'Aperta'])
    const pressed = [...host.querySelectorAll('.pinnedwidget .pinbtn')].map((b) =>
      b.getAttribute('aria-pressed'),
    )
    expect(pressed).toEqual(['true', 'true'])
  })

  it('unpins from its own row, and the list goes when the last pin does', async () => {
    pins = [NOTES.b.id]
    await mount()
    expect(titles()).toEqual(['Archiviata'])
    await act(async () => {
      host.querySelector<HTMLButtonElement>('.pinnedwidget .pinbtn')!.click()
      await new Promise((r) => setTimeout(r, 0))
    })
    expect(calls).toContain('POST /notes/unpin')
    expect(pins).toEqual([])
    expect(host.querySelector('.pinnedwidget')).toBeNull()
  })

  it('keeps a pinned note out of the recent ones, and gives it back on unpin', async () => {
    localStorage.clear()
    pins = [NOTES.a.id]
    await mount(<RecentNotes query={{}} />)
    await act(async () => {
      host.querySelector<HTMLButtonElement>('.recentwidget__toggle')!.click()
      await new Promise((r) => setTimeout(r, 0))
    })
    // The recent notes are asked of the server by the last time worked on.
    expect(calls.some((c) => c.startsWith('GET /notes?') && c.includes('order_by=touched_at'))).toBe(
      true,
    )
    const recent = () =>
      [...host.querySelectorAll('.recentwidget:not(.pinnedwidget) .recentrow__title')].map(
        (e) => e.textContent,
      )
    expect(titles()).toEqual(['Aperta'])
    expect(recent()).toEqual(['Archiviata'])
    await act(async () => {
      host.querySelector<HTMLButtonElement>('.pinnedwidget .pinbtn')!.click()
      await new Promise((r) => setTimeout(r, 0))
    })
    expect(host.querySelector('.pinnedwidget')).toBeNull()
    expect([...recent()].sort()).toEqual(['Aperta', 'Archiviata'])
  })

  it('pins a note through the note endpoint, not the task one', async () => {
    await mount(
      <>
        <PinButton kind="note" id={NOTES.a.id} />
        <PinButton kind="task" id={NOTES.a.id} />
      </>,
    )
    const [noteBtn, taskBtn] = [...host.querySelectorAll<HTMLButtonElement>('.pinbtn')]
    await act(async () => {
      noteBtn.click()
      await new Promise((r) => setTimeout(r, 0))
    })
    expect(calls).toContain('POST /notes/pin')
    expect(calls).not.toContain('POST /tasks/pin')
    expect(noteBtn.getAttribute('aria-pressed')).toBe('true')
    expect(taskBtn.getAttribute('aria-pressed')).toBe('false')
    expect(titles()).toEqual(['Aperta'])
  })

  it('drops a note put in the bin from another screen, and takes it back on restore', async () => {
    pins = [NOTES.a.id, NOTES.b.id]
    await mount()
    expect(titles()).toEqual(['Aperta', 'Archiviata'])
    // The delete a list or a note page makes: nothing here asks the pin
    // store to re-read.
    await act(async () => {
      await api.POST('/notes/{note_id}/delete', {
        params: { header: workspaceHeader(), path: { note_id: NOTES.a.id } },
        body: { expected_version: 1 },
      })
      await new Promise((r) => setTimeout(r, 0))
    })
    expect(titles()).toEqual(['Archiviata'])
    await act(async () => {
      await api.POST('/notes/{note_id}/restore', {
        params: { header: workspaceHeader(), path: { note_id: NOTES.a.id } },
        body: { expected_version: 2 },
      })
      await new Promise((r) => setTimeout(r, 0))
    })
    expect(titles()).toEqual(['Aperta', 'Archiviata'])
  })

  it('undoes a refused press and says why, even when the read after it fails', async () => {
    pins = [NOTES.a.id]
    await mount(<PinButton kind="note" id={NOTES.b.id} />)
    const btn = () =>
      [...host.querySelectorAll<HTMLButtonElement>('.pinbtn')].find(
        (b) => !b.closest('.pinnedwidget'),
      )!
    expect(btn().getAttribute('aria-pressed')).toBe('false')
    failWrite = {
      status: 409,
      body: { code: 'note.pin_limit', detail: 'You already have 200 pinned notes.' },
    }
    failRead = true
    await act(async () => {
      btn().click()
      await new Promise((r) => setTimeout(r, 0))
    })
    expect(calls).toContain('POST /notes/pin')
    expect(btn().getAttribute('aria-pressed')).toBe('false')
    // The pins the store held survive a failed read, and the refusal is said.
    expect(titles()).toEqual(['Aperta'])
    expect(host.querySelector('[role="alert"]')?.textContent).toBe(
      'You already have 200 pinned notes.',
    )
  })

  it('re-reads after an erase, a withdrawn atom and an emptied bin, from any screen', async () => {
    pins = [NOTES.a.id, NOTES.b.id]
    await mount(<PinButton kind="task" id={NOTES.a.id} />)
    expect(titles()).toEqual(['Aperta', 'Archiviata'])
    const post = async (path: string, body: unknown) => {
      await act(async () => {
        await api.POST(path as '/notes/merge', {
          params: { header: workspaceHeader(), path: { note_id: NOTES.a.id } },
          body: body as never,
        } as never)
        await new Promise((r) => setTimeout(r, 0))
      })
    }
    await post(`/notes/${NOTES.a.id}/erase`, {})
    expect(titles()).toEqual(['Archiviata'])
    await post('/garden/review/restore-source', { atom_note_id: NOTES.b.id })
    expect(host.querySelector('.pinnedwidget')).toBeNull()
    const reads = () => calls.filter((c) => c.startsWith('GET /notes?pinned=true')).length
    const taskReads = () => calls.filter((c) => c.startsWith('GET /tasks?pinned=true')).length
    const [n0, t0] = [reads(), taskReads()]
    await post('/workspaces/me/trash/empty', {})
    // Both kinds are read again: the bin held notes and tasks.
    expect(reads()).toBe(n0 + 1)
    expect(taskReads()).toBe(t0 + 1)
  })

  it('a read asked for while an older one is out is made again, not shared', async () => {
    pins = [NOTES.a.id]
    await mount()
    expect(titles()).toEqual(['Aperta'])
    let open!: () => void
    gate = new Promise<void>((r) => (open = r))
    const first = notePins.reload()
    await new Promise((r) => setTimeout(r, 0))
    // The server changes after the first read took its answer...
    pins = []
    const second = notePins.reload()
    gate = null
    open()
    await act(async () => {
      await Promise.all([first, second])
    })
    // ...and the second read reflects the change instead of the stale answer.
    expect(host.querySelector('.pinnedwidget')).toBeNull()
  })

  it('matches every listed request, and nothing else', () => {
    for (const { path, kinds } of PIN_MOVING_REQUESTS) {
      const sent = path.replace(/\{[^}]+\}/g, '00000000-0000-4000-8000-000000000009')
      expect(pinKindsMovedBy(sent)).toEqual(kinds)
    }
    for (const other of [
      '/notes/x/archive',
      '/notes/x/delete/more',
      '/notes/x/y/delete',
      '/notes/pin',
      '/tasks/x/purge',
      '/garden/review/reject',
    ]) {
      expect(pinKindsMovedBy(other)).toEqual([])
    }
  })
})
