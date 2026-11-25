import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { NoteHistoryPanel } from './NoteHistoryPanel'
import { clearSession, setSession } from '../auth/session'
import i18n from '../i18n'

// ADR-0075 decision 5: the history says in words what happened, and who did
// it: the person, an agent, or the garden on its own. It reads nothing until
// it is opened.

const fetchMock = vi.hoisted(() => {
  const fn = vi.fn<(...args: unknown[]) => Promise<Response>>()
  globalThis.fetch = fn as unknown as typeof globalThis.fetch
  return fn
})

const NOTE = '00000000-0000-4000-8000-000000000001'
const ATOM = '00000000-0000-4000-8000-000000000002'
let calls = 0

beforeEach(async () => {
  await i18n.changeLanguage('it')
  setSession({ token: 't', workspaceId: '00000000-0000-4000-8000-0000000000aa' })
  calls = 0
  fetchMock.mockImplementation(async () => {
    calls += 1
    return new Response(
      JSON.stringify({
        items: [
          {
            event_id: '00000000-0000-4000-8000-0000000000e1',
            at: '2026-11-24T10:00:00Z',
            action: 'auto_maturity',
            actor_kind: 'system',
            changes: { maturity: { old: 'growing', new: 'mature' } },
          },
          {
            event_id: '00000000-0000-4000-8000-0000000000e2',
            at: '2026-11-23T10:00:00Z',
            action: 'garden_review:source_humus',
            actor_kind: 'human_api',
            changes: { humus_flag: { old: false, new: true } },
            about_note_id: ATOM,
          },
        ],
        next_cursor: null,
      }),
      { status: 200, headers: { 'content-type': 'application/json' } },
    )
  })
})

afterEach(() => {
  clearSession()
  fetchMock.mockReset()
})

describe('NoteHistoryPanel', () => {
  it('reads on opening and says what happened, by whom', async () => {
    const host = document.createElement('div')
    document.body.appendChild(host)
    const root: Root = createRoot(host)
    await act(async () =>
      root.render(
        <MemoryRouter>
          <NoteHistoryPanel noteId={NOTE} />
        </MemoryRouter>,
      ),
    )
    expect(calls).toBe(0)
    const details = host.querySelector('details')!
    await act(async () => {
      details.open = true
      details.dispatchEvent(new Event('toggle'))
    })
    for (let i = 0; i < 3; i++) await act(async () => new Promise((r) => setTimeout(r, 0)))
    expect(calls).toBe(1)
    const text = host.textContent ?? ''
    expect(text).toContain(
      i18n.t('noteHistory.action.auto_maturity', {
        from: i18n.t('garden.maturity.growing'),
        to: i18n.t('garden.maturity.mature'),
      }),
    )
    expect(text).toContain(i18n.t('noteHistory.who.garden'))
    expect(text).toContain(i18n.t('noteHistory.who.person'))
    expect(host.querySelector(`a[href="/garden/review/${ATOM}"]`)).not.toBeNull()
    act(() => root.unmount())
    host.remove()
  })
})
