import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { PinnedTasks } from './PinnedTasks'
import { resetPinsForTest } from '../api/pins'
import { clearSession, setSession } from '../auth/session'
import '../i18n'

// What the pinned list promises (migration 0027), against a double of the
// API that keeps its own set of pins, so a press is answered the way the
// server answers it:
// - nothing at all is drawn while nothing is pinned;
// - pins come back newest first, and a closed task is listed like any other;
// - the pin control on a row of this list takes the task off it, and says
//   so to the server.

// Installed before the imports run: openapi-fetch reads `globalThis.fetch`
// when the client module is created.
const fetchMock = vi.hoisted(() => {
  const fn = vi.fn<(...args: unknown[]) => Promise<Response>>()
  globalThis.fetch = fn as unknown as typeof globalThis.fetch
  return fn
})

type T = { id: string; title: string; state: string }
const TASKS: Record<string, T> = {
  a: { id: '00000000-0000-4000-8000-00000000000a', title: 'Aperto', state: 'todo' },
  b: { id: '00000000-0000-4000-8000-00000000000b', title: 'Chiuso', state: 'done' },
}

let pins: string[] = []
const calls: string[] = []

function row(t: T) {
  return {
    ...t,
    state_id: 's',
    version: 1,
    priority: 1,
    importance: 1,
    urgency: 1,
    tags: [],
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
    if (req.method === 'GET' && path === '/tasks') {
      return json(pins.map((id) => row(Object.values(TASKS).find((t) => t.id === id)!)))
    }
    const { task_id } = (await req.json()) as { task_id: string }
    if (path === '/tasks/pin') pins = [task_id, ...pins.filter((x) => x !== task_id)]
    if (path === '/tasks/unpin') pins = pins.filter((x) => x !== task_id)
    return json({ task_id, pinned: path === '/tasks/pin' })
  })
}

let host: HTMLDivElement
let root: Root

async function mount(): Promise<void> {
  await act(async () => {
    root.render(
      <MemoryRouter>
        <PinnedTasks tasks={[]} />
      </MemoryRouter>,
    )
  })
  // Let the store's first read land.
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

describe('PinnedTasks', () => {
  it('draws nothing at all while nothing is pinned', async () => {
    await mount()
    expect(host.querySelector('.pinnedwidget')).toBeNull()
    expect(calls).toContain('GET /tasks?pinned=true')
  })

  it('lists the pins newest first, a closed task like any other', async () => {
    pins = [TASKS.b.id, TASKS.a.id]
    await mount()
    expect(titles()).toEqual(['Chiuso', 'Aperto'])
    const pressed = [...host.querySelectorAll('.pinnedwidget .pinbtn')].map((b) =>
      b.getAttribute('aria-pressed'),
    )
    expect(pressed).toEqual(['true', 'true'])
  })

  it('unpins from its own row, and the list goes when the last pin does', async () => {
    pins = [TASKS.b.id]
    await mount()
    expect(titles()).toEqual(['Chiuso'])
    const btn = host.querySelector<HTMLButtonElement>('.pinnedwidget .pinbtn')!
    await act(async () => {
      btn.click()
      await new Promise((r) => setTimeout(r, 0))
    })
    expect(calls).toContain('POST /tasks/unpin')
    expect(pins).toEqual([])
    expect(host.querySelector('.pinnedwidget')).toBeNull()
  })
})
