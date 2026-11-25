import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { GardenDelegationSettings } from './GardenDelegationSettings'
import { clearSession, setSession } from '../auth/session'
import i18n from '../i18n'

// ADR-0076 phase B: the person hands decisions over one kind at a time.
// Nothing is checked until they choose; saving writes exactly the chosen
// kinds to the workspace setting; the count of the agents' decisions and of
// the person's reversals stands beside each kind.

const fetchMock = vi.hoisted(() => {
  const fn = vi.fn<(...args: unknown[]) => Promise<Response>>()
  globalThis.fetch = fn as unknown as typeof globalThis.fetch
  return fn
})

const WS = '00000000-0000-4000-8000-0000000000aa'
let stored: string[] = []
const patches: Record<string, unknown>[] = []

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
    const path = new URL(req.url, 'http://t').pathname.replace(/^\/api/, '')
    if (req.method === 'GET' && path === '/workspaces/me') {
      return json({
        id: WS,
        name: 'w',
        version: 4,
        settings: { estimate_presets: ['1'], garden_delegation: stored },
      })
    }
    if (req.method === 'GET' && path === '/garden/delegation/stats') {
      return json([{ kind: 'synthesis_decline', decided: 3, reversed: 1 }])
    }
    if (req.method === 'PATCH' && path === '/workspaces/me/settings') {
      const body = (await req.json()) as Record<string, unknown>
      patches.push(body)
      stored = body.garden_delegation as string[]
      return json({ id: WS, version: 5 })
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

beforeEach(async () => {
  await i18n.changeLanguage('it')
  setSession({ token: 't', workspaceId: WS })
  stored = []
  patches.length = 0
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

const box = (kind: string) =>
  [...host.querySelectorAll<HTMLLabelElement>('label')].find((l) =>
    l.textContent?.includes(i18n.t(`gardenDelegation.kind.${kind}.name`)),
  )!.querySelector<HTMLInputElement>('input[type="checkbox"]')!

describe('GardenDelegationSettings', () => {
  it('starts with nothing handed over, and saves exactly what the person chose', async () => {
    await act(async () => root.render(<GardenDelegationSettings />))
    await settle()
    const all = [...host.querySelectorAll<HTMLInputElement>('input[type="checkbox"]')]
    expect(all).toHaveLength(5)
    expect(all.every((b) => !b.checked)).toBe(true)
    expect(host.textContent).toContain(
      i18n.t('gardenDelegation.stats', { decided: 3, reversed: 1 }),
    )

    await act(async () => box('synthesis_decline').click())
    await act(async () => box('link_proposal_reject').click())
    const save = [...host.querySelectorAll('button')].find(
      (b) => b.textContent === i18n.t('wsmgr.save'),
    )!
    await act(async () => save.click())
    await settle()
    expect(patches).toHaveLength(1)
    expect([...(patches[0].garden_delegation as string[])].sort()).toEqual([
      'link_proposal_reject',
      'synthesis_decline',
    ])
  })
})
