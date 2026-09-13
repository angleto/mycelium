import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom'
import { PeekButton } from './PeekButton'
import { clearSession, setSession } from '../auth/session'
import '../i18n'

// The promise of the eye: read the row without leaving the list.
//
// So the assertion that carries the feature is the NEGATIVE one — the
// route does not change — and after it, that what the dialog shows is the
// entity's own body rather than the row's preview, which is why it fetches
// at all.
//
// The stub has to be installed BEFORE this module's imports run, not in a
// `beforeEach`: openapi-fetch destructures `globalThis.fetch` when the
// client is created, which happens while `api/client` is being imported.
// A later `vi.stubGlobal('fetch', …)` replaces a reference nobody reads,
// and the suite then quietly talks to the real network.
const fetchMock = vi.hoisted(() => {
  const fn = vi.fn<(...args: unknown[]) => Promise<Response>>()
  globalThis.fetch = fn as unknown as typeof globalThis.fetch
  return fn
})

let host: HTMLDivElement
let root: Root

/** The current route, rendered rather than captured into a variable: a
 *  component that writes to something outside itself during render is
 *  impure, and the lint rules say so. */
function Probe() {
  return <p className="probe-path">{useLocation().pathname}</p>
}

function path(): string {
  return host.querySelector('.probe-path')?.textContent ?? ''
}

const TASK = {
  id: '7b2ea76f-a08e-460c-854f-1ec265aceaec',
  title: 'Una card',
  description: 'il **corpo** del task',
  state: 'todo',
  tags: [],
  checklist: [
    { id: 'c1', text: 'fatto', done: true },
    { id: 'c2', text: 'da fare', done: false },
  ],
}

function answer(body: unknown, status = 200): void {
  fetchMock.mockResolvedValue(
    new Response(JSON.stringify(body), {
      status,
      headers: { 'content-type': 'application/json' },
    }),
  )
}

async function open(): Promise<HTMLElement> {
  const eye = host.querySelector('button.peek')
  if (!(eye instanceof HTMLElement)) throw new Error('no peek button')
  await act(async () => eye.click())
  // The response lands a microtask after the click; act() flushes React,
  // not the network.
  await act(async () => {
    await Promise.resolve()
  })
  const dialog = document.querySelector('[role="dialog"]')
  if (!(dialog instanceof HTMLElement)) throw new Error('no dialog')
  return dialog
}

function mount(): void {
  act(() =>
    root.render(
      <MemoryRouter initialEntries={['/tasks?q=una']}>
        <Probe />
        <Routes>
          <Route
            path="/tasks"
            element={
              <PeekButton target={{ kind: 'task', id: TASK.id, title: TASK.title }} />
            }
          />
          <Route path="/tasks/:id" element={<p>detail</p>} />
        </Routes>
      </MemoryRouter>,
    ),
  )
}

beforeEach(() => {
  // Every surface in this app runs under RequireAuth, and the workspace
  // header is read from the session: without one the request cannot even
  // be addressed.
  setSession({ token: 't', workspaceId: '00000000-0000-4000-8000-000000000001' })
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

describe('PeekButton', () => {
  it('opens a dialog over the list without navigating away from it', async () => {
    answer(TASK)
    mount()
    expect(path()).toBe('/tasks')
    const dialog = await open()
    expect(dialog.textContent).toContain('Una card')
    // The whole point: the list route is still the route.
    expect(path()).toBe('/tasks')
  })

  it('shows the entity body, which the row does not carry', async () => {
    answer(TASK)
    mount()
    const dialog = await open()
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const req = fetchMock.mock.calls[0][0] as Request
    expect(req.url).toContain(`/tasks/${TASK.id}`)
    // Rendered as markdown, not printed as source.
    expect(dialog.querySelector('.md strong')?.textContent).toBe('corpo')
    // And the checklist, which every list payload leaves empty.
    expect(dialog.querySelectorAll('.peek__checklist li')).toHaveLength(2)
  })

  it('reports a failed read instead of staying on "loading" forever', async () => {
    answer({ detail: 'Task not found' }, 404)
    mount()
    const dialog = await open()
    expect(dialog.querySelector('.err')?.textContent).toBe('Task not found')
  })

  it('reports a request that never reached the server at all', async () => {
    // A throw rather than a response. It used to escape the effect as an
    // unhandled rejection and leave the dialog on "Loading…".
    fetchMock.mockRejectedValue(new Error('network down'))
    mount()
    const dialog = await open()
    expect(dialog.querySelector('.err')).not.toBeNull()
  })
})
