import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { GardenSuggestionsPanel } from './GardenSuggestionsPanel'
import { clearSession, setSession } from '../auth/session'
import i18n from '../i18n'

// ADR-0075 decision 4: the suggestions on a note were a bare check mark and
// cross, with the reason in a hover title. Each control now names what it
// does, and the reason is on the row.

const fetchMock = vi.hoisted(() => {
  const fn = vi.fn<(...args: unknown[]) => Promise<Response>>()
  globalThis.fetch = fn as unknown as typeof globalThis.fetch
  return fn
})

const NOTE = '00000000-0000-4000-8000-000000000001'
const TAG = '00000000-0000-4000-8000-000000000002'
const writes: Record<string, unknown>[] = []

function json(body: unknown): Promise<Response> {
  return Promise.resolve(
    new Response(JSON.stringify(body), {
      status: 200,
      headers: { 'content-type': 'application/json' },
    }),
  )
}

beforeEach(async () => {
  await i18n.changeLanguage('it')
  setSession({ token: 't', workspaceId: '00000000-0000-4000-8000-0000000000aa' })
  writes.length = 0
  fetchMock.mockImplementation(async (input: unknown) => {
    const req = input as Request
    const path = new URL(req.url, 'http://t').pathname.replace(/^\/api/, '')
    if (path.startsWith('/garden/classify/')) {
      return json({
        node_id: NOTE,
        tags: [{ tag_id: TAG, confidence: 0.8, rationale: 'shares three terms with #corsa' }],
        links: [],
        maturity: {
          value: 'mature',
          confidence: 0.7,
          rationale: 'linked from five notes',
          auto_apply: false,
        },
        cluster: null,
        signals_used: [],
        source: 'live',
        computed_at: '2026-11-24T10:00:00Z',
      })
    }
    if (path === '/tags') return json([{ id: TAG, name: 'corsa', kind: 'generic' }])
    if (req.method === 'POST' && path === '/garden/apply') {
      writes.push((await req.json()) as Record<string, unknown>)
      return json({ applied: true })
    }
    return json({ items: [] })
  })
})

afterEach(() => {
  clearSession()
  fetchMock.mockReset()
})

describe('GardenSuggestionsPanel', () => {
  it('names each action and shows each reason, with no bare glyph control', async () => {
    const host = document.createElement('div')
    document.body.appendChild(host)
    const root: Root = createRoot(host)
    await act(async () => root.render(<GardenSuggestionsPanel nodeId={NOTE} />))
    for (let i = 0; i < 4; i++) await act(async () => new Promise((r) => setTimeout(r, 0)))

    const names = [...host.querySelectorAll('button')].map((b) => b.textContent?.trim())
    expect(names).toContain(i18n.t('gardenSuggest.addTag'))
    expect(names).toContain(i18n.t('gardenSuggest.promoteMature'))
    expect(names).toContain(i18n.t('gardenSuggest.dismiss'))
    expect(names).not.toContain('✓')
    expect(names).not.toContain('×')
    expect(host.textContent).toContain('shares three terms with #corsa')
    expect(host.textContent).toContain('linked from five notes')

    const add = [...host.querySelectorAll('button')].find(
      (b) => b.textContent?.trim() === i18n.t('gardenSuggest.addTag'),
    )!
    await act(async () => add.click())
    for (let i = 0; i < 3; i++) await act(async () => new Promise((r) => setTimeout(r, 0)))
    expect(writes[0]).toMatchObject({ suggestion_type: 'tag', action: 'accept' })
    act(() => root.unmount())
    host.remove()
  })
})
