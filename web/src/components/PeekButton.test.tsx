import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom'
import { PeekButton } from './PeekButton'
import { clearSession, setSession } from '../auth/session'
import '../i18n'

// The promise of the eye: open the row without leaving the list.
//
// Two assertions carry it. The NEGATIVE one — the route does not change —
// and the one that says WHAT opened: the detail screen itself, not a second
// rendering of it. The first version of this dialog drew its own markdown
// view and its own checkbox list, and the way that shows in a test is that
// there is nothing to assert except markup this file invented. So the probe
// is the editor toolbar and the properties column, which only the real
// screen has, plus the absence of the back link, which only a page has.
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

// This file mounts the REAL detail screen, which is the point of the
// dialog and also what it costs: eleven requests answered by the double,
// the markdown editor, the properties column and the tab strip, all under
// jsdom. Measured on 2026-09-14 inside the full suite: 13.3s for the first
// test (it pays the module init too), 4.5s and 3.7s for the next two, and
// 108ms for the one that short-circuits on a 404. The default 5s budget
// therefore fails whenever the machine is loaded, and passed in isolation,
// which is the shape of a flake rather than of a defect. The budget is
// raised here, in the one file that needs it, instead of globally.
vi.setConfig({ testTimeout: 30_000 })

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

const STATE_ID = '00000000-0000-4000-8000-0000000000aa'
const TASK = {
  id: '7b2ea76f-a08e-460c-854f-1ec265aceaec',
  title: 'Una card',
  description: 'il **corpo** del task',
  state: 'todo',
  state_id: STATE_ID,
  version: 1,
  tags: [],
  checklist: [
    { id: 'c1', text: 'fatto', done: true },
    { id: 'c2', text: 'da fare', done: false },
  ],
}

/** Endpoints whose payload is an OBJECT. Everything else in this screen is
 *  a collection, and the difference is not cosmetic: `/garden/classify`
 *  returns `{tags, links, maturity}` and its panel reads `.tags.length`
 *  straight away. */
const OBJECT_ENDPOINTS = new Set([
  `/tasks/${TASK.id}`,
  '/workspaces/me',
  `/garden/classify/${TASK.id}`,
])

/** The detail screen is not one request: it asks for the task, its states,
 *  the tags, the projects, the sibling tasks, the dependencies, the
 *  workspace, the reminders, the notes, the relations and its own
 *  annotations. A double that answers only the first would put the screen
 *  in a permanent loading state and prove nothing, so this answers each by
 *  path with the emptiest shape that endpoint really returns. */
function answerAll(task: unknown = TASK): void {
  fetchMock.mockImplementation((input: unknown) => {
    // The client prefixes every path with /api; the set below names the
    // endpoints, not the mount point.
    const url = new URL(
      typeof input === 'string' ? input : (input as Request).url,
      'http://t',
    ).pathname.replace(/^\/api/, '')
    // Shape matters, not just status: a panel that reads `data.tags.length`
    // crashes on `[]` exactly as it would on a wrong payload in production,
    // which is the contract a double owes (TST-01).
    const body = OBJECT_ENDPOINTS.has(url)
      ? url === `/tasks/${TASK.id}`
        ? task
        : url === '/workspaces/me'
          ? { id: 'w', name: 'W', settings: {} }
          : { tags: [], links: [], maturity: null, signals_used: [] }
      : url === `/tasks/${TASK.id}/states`
        ? [{ id: STATE_ID, name: 'todo', ord: 0, is_terminal: false }]
        : []
    return Promise.resolve(
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }),
    )
  })
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
  it('opens over the list without navigating away from it', async () => {
    answerAll()
    mount()
    expect(path()).toBe('/tasks')
    const dialog = await open()
    expect(dialog.textContent).toContain('Una card')
    // The whole point: the list route is still the route.
    expect(path()).toBe('/tasks')
  })

  it('holds the detail screen itself, not a second rendering of it', async () => {
    answerAll()
    mount()
    const dialog = await open()
    // Things only the real screen has. A read-only imitation had none of
    // them, which is what made it look like a worse version of the page.
    expect(dialog.querySelector('.taskdetail')).not.toBeNull()
    expect(dialog.querySelector('.taskdetail__main')).not.toBeNull()
    expect(dialog.querySelector('[role="tablist"], .tabs__tab')).not.toBeNull()
    // And the thing only a PAGE has: the way back to the list. In a dialog
    // the list is behind the modal, so a link to it is a second exit that
    // leaves the modal open over a route that changed underneath.
    expect(dialog.querySelector('.taskdetail__back')).toBeNull()
  })

  it('asks for the task it was given, not for the row it was rendered from', async () => {
    answerAll()
    mount()
    await open()
    const urls = fetchMock.mock.calls.map((c) =>
      typeof c[0] === 'string' ? c[0] : (c[0] as Request).url,
    )
    expect(urls.some((u) => u.includes(`/tasks/${TASK.id}`))).toBe(true)
  })

  it('reports a failed read instead of staying on "loading" forever', async () => {
    // A factory, not one Response: the screen makes many calls and a body
    // can only be read once.
    fetchMock.mockImplementation(() =>
      Promise.resolve(
        new Response(JSON.stringify({ detail: 'Task not found' }), {
          status: 404,
          headers: { 'content-type': 'application/json' },
        }),
      ),
    )
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
    expect(dialog.textContent).not.toContain('Loading')
  })
})
