import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { FocusHereButton } from './FocusHereButton'
import { FocusProvider, useFocus } from '../lib/focus'
import { clearSession, setSession } from '../auth/session'
import '../i18n'

// "Focus on this" from a task or note, and the way back out.
//
// What the user relies on is the STORED focus, because every scoped view
// reads it from the context and a reload reads it from localStorage. So the
// assertions are on both: the context as a probe renders it, and the
// per-workspace record a reload would rehydrate from.

const WS = '00000000-0000-4000-8000-000000000001'
const CLIENT = { id: 'c0000000-0000-4000-8000-000000000001', kind: 'client', name: 'Acme', color: null } as const
const PROJECT = { id: 'p0000000-0000-4000-8000-000000000001', kind: 'project', name: 'Portal', color: null } as const
const OTHER = { id: 'c0000000-0000-4000-8000-000000000002', kind: 'client', name: 'Globex', color: null } as const

let host: HTMLDivElement
let root: Root

function Probe() {
  const f = useFocus()
  return (
    <p className="probe">
      {f.active ? `${f.clientId}|${f.clientName}|${f.projectId}` : 'all'}
    </p>
  )
}

function Clear() {
  const { clear } = useFocus()
  return (
    <button type="button" className="probe-clear" onClick={clear}>
      x
    </button>
  )
}

type Tags = Parameters<typeof FocusHereButton>[0]['tags']

function mount(tags: Tags) {
  act(() =>
    root.render(
      <FocusProvider>
        <FocusHereButton tags={tags} />
        <Probe />
        <Clear />
      </FocusProvider>,
    ),
  )
}

const button = () => host.querySelector<HTMLButtonElement>('button.focushere')
const probe = () => host.querySelector('.probe')?.textContent
const stored = () => localStorage.getItem(`mycelium-focus:${WS}`)
const click = (el: Element | null) =>
  act(() => (el as HTMLButtonElement).click())

beforeEach(() => {
  localStorage.clear()
  setSession({ token: 't', workspaceId: WS })
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
})

afterEach(() => {
  act(() => root.unmount())
  host.remove()
  clearSession()
  localStorage.clear()
})

describe('FocusHereButton', () => {
  it('focuses the client AND the project of the item in one step', () => {
    mount([CLIENT, PROJECT])
    expect(probe()).toBe('all')
    expect(button()?.getAttribute('aria-pressed')).toBe('false')

    click(button())

    expect(probe()).toBe(`${CLIENT.id}|Acme|${PROJECT.id}`)
    expect(JSON.parse(stored() ?? '{}')).toEqual({
      clientId: CLIENT.id,
      clientName: 'Acme',
      projectId: PROJECT.id,
    })
    expect(button()?.getAttribute('aria-pressed')).toBe('true')
  })

  it('pressed again, leaves focus and forgets the record', () => {
    mount([CLIENT, PROJECT])
    click(button())
    click(button())
    expect(probe()).toBe('all')
    expect(stored()).toBeNull()
    expect(button()?.getAttribute('aria-pressed')).toBe('false')
  })

  it('an item without a project focuses its whole client', () => {
    mount([CLIENT])
    click(button())
    expect(probe()).toBe(`${CLIENT.id}|Acme|`)
    expect(button()?.getAttribute('aria-pressed')).toBe('true')
  })

  it('is not pressed when the focus is on the same client but another scope', () => {
    // Focus on the whole client, then look at a task of one of its projects:
    // pressing must narrow to the project, not read as "already focused".
    mount([CLIENT])
    click(button())
    mount([CLIENT, PROJECT])
    expect(button()?.getAttribute('aria-pressed')).toBe('false')
    click(button())
    expect(probe()).toBe(`${CLIENT.id}|Acme|${PROJECT.id}`)
  })

  it('replaces a focus on another client', () => {
    mount([OTHER])
    click(button())
    mount([CLIENT, PROJECT])
    click(button())
    expect(probe()).toBe(`${CLIENT.id}|Acme|${PROJECT.id}`)
  })

  it('renders nothing for an item with no client tag', () => {
    mount([])
    expect(button()).toBeNull()
    mount(undefined)
    expect(button()).toBeNull()
  })
})

describe('focus clear', () => {
  it('returns to all clients from any focus', () => {
    mount([CLIENT, PROJECT])
    click(button())
    click(host.querySelector('.probe-clear'))
    expect(probe()).toBe('all')
    expect(stored()).toBeNull()
  })
})
