// The assistants list says who holds each token (ADR-0069).
//
// An assistant created by an OAuth consent is a bearer some outside client
// holds. The person reviewing the list must be able to see which client,
// where its codes go and when it was approved, and when the token stops
// working, or they cannot decide what to revoke. ``fetch`` is the only thing
// replaced.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { AiAssistantsSettings } from './AiAssistantsSettings'
import { setSession } from '../auth/session'
import i18n from '../i18n'

let host: HTMLDivElement
let root: Root

const BASE = {
  provider: null,
  model_id: null,
  notes: null,
  scope: ['tasks:read'],
  is_active: true,
  runtime: 'external',
  version: 1,
  created_at: '2026-11-13T10:00:00Z',
  updated_at: '2026-11-13T10:00:00Z',
  token_prefix: 'mycelium_at_ab',
  workspace_binding: 'workspace',
}

beforeEach(async () => {
  await i18n.changeLanguage('en')
  setSession({ token: 'person-jwt', workspaceId: 'ws-1' })
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      const body =
        url === '/api/ai-assistants'
          ? [
              {
                ...BASE,
                id: 'a1',
                label: 'Claude via claude.ai',
                token_expires_at: '2027-11-13T10:00:00Z',
                oauth: {
                  client_id: 'mycelium_pc_x',
                  client_name: 'Claude',
                  client_registration: 'dynamic',
                  redirect_target: 'https://claude.ai',
                  approved_by_id: 'u1',
                  approved_at: '2026-11-13T10:00:00Z',
                },
              },
              { ...BASE, id: 'a2', label: 'By hand', token_expires_at: null, oauth: null },
            ]
          : { mcp_url: '/mcp', instructions_md: 'paste' }
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }),
  )
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
})

afterEach(() => {
  act(() => root.unmount())
  host.remove()
  vi.unstubAllGlobals()
})

describe('AiAssistantsSettings', () => {
  it('shows who holds an OAuth assistant and when its token expires', async () => {
    await act(async () => {
      root.render(<AiAssistantsSettings />)
    })
    for (let i = 0; i < 5; i++) {
      await act(async () => {
        await Promise.resolve()
      })
    }
    const text = host.textContent ?? ''
    expect(text).toContain('Connected through OAuth by Claude (https://claude.ai)')
    expect(text).toContain(`token expires ${new Date('2027-11-13T10:00:00Z').toLocaleDateString()}`)
    // An assistant made by hand has no provenance line.
    expect(text.match(/Connected through OAuth/g)?.length).toBe(1)
  })
})
