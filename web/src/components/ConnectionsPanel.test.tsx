import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { ConnectionsPanel } from './ConnectionsPanel'
import { clearSession, setSession } from '../auth/session'
import i18n from '../i18n'

// What the connections panel promises (ADR-0077), against a double of the
// API: every connection grouped by meaning with its direction in words; a
// row that can be removed has Disconnect, a mention and a promotion say why
// they cannot; Connect offers only the relations the server lists, and sends
// the relation, the direction and the other item; a dependency also sends
// its type and wait.

const fetchMock = vi.hoisted(() => {
  const fn = vi.fn<(...args: unknown[]) => Promise<Response>>()
  globalThis.fetch = fn as unknown as typeof globalThis.fetch
  return fn
})

const id = (n: number) => `00000000-0000-4000-8000-0000000000${String(n).padStart(2, '0')}`
const ME = id(1)
const writes: { path: string; body: Record<string, unknown> }[] = []

function json(body: unknown): Promise<Response> {
  return Promise.resolve(
    new Response(JSON.stringify(body), {
      status: 200,
      headers: { 'content-type': 'application/json' },
    }),
  )
}

const ITEMS = [
  {
    handle: 'parent:x',
    relation: 'subtask_of',
    direction: 'out',
    other_kind: 'task',
    other_id: id(2),
    other_title: 'Release',
    removable: true,
    detail: {},
  },
  {
    handle: 'dep:y',
    relation: 'depends_on',
    direction: 'out',
    other_kind: 'task',
    other_id: id(3),
    other_title: 'Build',
    removable: true,
    detail: { type: 'FS', lag_minutes: 30 },
  },
  {
    handle: 'mention:in:note:z',
    relation: 'mentions',
    direction: 'in',
    other_kind: 'note',
    other_id: id(4),
    other_title: 'Diario',
    removable: false,
    detail: {},
  },
]
const CHOICES = [
  { relation: 'related', direction: 'none', other_kind: 'task' },
  { relation: 'subtask_of', direction: 'in', other_kind: 'task' },
  { relation: 'depends_on', direction: 'out', other_kind: 'task' },
]

beforeEach(async () => {
  await i18n.changeLanguage('it')
  setSession({ token: 't', workspaceId: '00000000-0000-4000-8000-0000000000aa' })
  writes.length = 0
  fetchMock.mockImplementation(async (input: unknown) => {
    const req = input as Request
    const path = new URL(req.url, 'http://t').pathname.replace(/^\/api/, '')
    if (req.method === 'GET' && path === '/connections') return json({ items: ITEMS, choices: CHOICES })
    if (req.method === 'GET' && path === '/tasks') {
      return json([
        { id: id(9), title: 'Deploy', state_id: null, tags: [], is_archived: false },
      ])
    }
    if (req.method === 'GET') return json([])
    writes.push({ path, body: (await req.json()) as Record<string, unknown> })
    return json({ done: true })
  })
})

afterEach(() => {
  clearSession()
  fetchMock.mockReset()
})

async function settle(): Promise<void> {
  for (let i = 0; i < 4; i++) await act(async () => new Promise((r) => setTimeout(r, 0)))
}

describe('ConnectionsPanel', () => {
  it('groups by meaning, says the direction, and offers Disconnect only where it works', async () => {
    const host = document.createElement('div')
    document.body.appendChild(host)
    const root: Root = createRoot(host)
    await act(async () =>
      root.render(
        <MemoryRouter>
          <ConnectionsPanel kind="task" id={ME} />
        </MemoryRouter>,
      ),
    )
    await settle()
    const heads = [...host.querySelectorAll('.connections__grouphead')].map((h) => h.textContent)
    expect(heads).toEqual([
      i18n.t('itemConnections.group.subtask_of.out'),
      i18n.t('itemConnections.group.depends_on.out'),
      i18n.t('itemConnections.group.mentions.in'),
    ])
    expect(host.textContent).toContain(i18n.t('itemConnections.depType.FS'))
    expect(host.textContent).toContain(i18n.t('itemConnections.lag', { minutes: 30 }))
    const disconnects = [...host.querySelectorAll('button')].filter(
      (b) => b.textContent === i18n.t('itemConnections.disconnect'),
    )
    expect(disconnects).toHaveLength(2)
    expect(host.textContent).toContain(i18n.t('itemConnections.fixed.mentions'))

    await act(async () => disconnects[0].click())
    await settle()
    expect(writes[0]).toEqual({
      path: '/connections/disconnect',
      body: { this_kind: 'task', this_id: ME, handle: 'parent:x', that_kind: 'task' },
    })

    const select = host.querySelector<HTMLSelectElement>('.connections__add select')!
    const options = [...select.options].map((o) => o.textContent)
    expect(options.slice(1)).toEqual([
      i18n.t('itemConnections.choice.task.related.none.task'),
      i18n.t('itemConnections.choice.task.subtask_of.in.task'),
      i18n.t('itemConnections.choice.task.depends_on.out.task'),
    ])
    await act(async () => {
      select.value = 'depends_on:out:task'
      select.dispatchEvent(new Event('change', { bubbles: true }))
    })
    await settle()
    expect(host.textContent).toContain(i18n.t('itemConnections.depTypeLabel'))
    act(() => root.unmount())
    host.remove()
  })
})
