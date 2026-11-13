// The OAuth consent page (ADR-0069), against a fake of the HTTP edge.
//
// What is pinned: the page shows the client's name AND the host the code
// will be sent to, read from the server's answer and not from the URL; it
// preselects the scope the server suggested; Approve sends the request id,
// label and ticked scope as a JSON POST with the session's bearer and then
// navigates to exactly the URL the server returned; Deny does the same with
// the deny route. ``fetch`` is the only thing replaced: the API module, the
// scope picker and the page are the real ones.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { OAuthConsentRoute } from './OAuthConsentRoute'
import { setSession } from '../auth/session'
import i18n from '../i18n'

// The active workspace comes from the SPA's shared store, which fetches
// through the typed client (bound to the real fetch at import, so the fake
// below would not reach it). Replaced with the snapshot it would hold.
vi.mock('../auth/useMyWorkspace', () => ({
  useMyWorkspace: () => ({ ws: { name: 'Studio' }, reload: async () => {} }),
}))

const REQUEST = 'req_0123456789abcdefghijklmnop'
const CODE_URL = 'https://evil-looking.example/cb?code=abc&state=xyz'
const DENY_URL = 'https://evil-looking.example/cb?error=access_denied&state=xyz'

type Call = { url: string; method: string; body: unknown; auth: string | null }

let host: HTMLDivElement
let root: Root
let calls: Call[]

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function fakeServer() {
  calls = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
      const method = (init?.method ?? (input instanceof Request ? input.method : 'GET')).toUpperCase()
      const headers = new Headers(init?.headers ?? (input instanceof Request ? input.headers : {}))
      const body = init?.body ? JSON.parse(String(init.body)) : null
      calls.push({ url, method, body, auth: headers.get('Authorization') })
      if (url.startsWith('/api/oauth/consent?')) {
        return json({
          client_id: 'mycelium_pc_x',
          client_name: 'Claude',
          registration: 'dynamic',
          redirect_uri: 'https://evil-looking.example/cb',
          redirect_target: 'https://evil-looking.example',
          expires_at: '2026-11-13T12:00:00Z',
          requested_scope: null,
          suggested_scope: ['tasks:read'],
        })
      }
      if (url === '/api/ai-assistants/scope-catalog') {
        return json([
          { key: 'tasks:read', category: 'read', label: 'Read tasks', description: '' },
          { key: 'notes:read', category: 'read', label: 'Read notes', description: '' },
        ])
      }
      if (url === '/api/oauth/consent/approve') return json({ redirect_url: CODE_URL })
      if (url === '/api/oauth/consent/deny') return json({ redirect_url: DENY_URL })
      return json({}, 404)
    }),
  )
}

async function settle() {
  for (let i = 0; i < 5; i++) {
    await act(async () => {
      await Promise.resolve()
    })
  }
}

async function render(navigate: (u: string) => void) {
  await act(async () => {
    root.render(
      <MemoryRouter initialEntries={[`/oauth/consent?request=${REQUEST}`]}>
        <Routes>
          <Route path="/oauth/consent" element={<OAuthConsentRoute navigate={navigate} />} />
        </Routes>
      </MemoryRouter>,
    )
  })
  await settle()
}

function button(name: string): HTMLButtonElement {
  const found = [...host.querySelectorAll('button')].find((b) => b.textContent === name)
  if (!found) throw new Error(`no button ${name}`)
  return found as HTMLButtonElement
}

beforeEach(async () => {
  await i18n.changeLanguage('en')
  setSession({ token: 'person-jwt', workspaceId: 'ws-1' })
  fakeServer()
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
})

afterEach(() => {
  act(() => root.unmount())
  host.remove()
  vi.unstubAllGlobals()
})

describe('OAuthConsentRoute', () => {
  it('shows the client name and, prominently, the scheme and host the code goes to', async () => {
    await render(() => {})
    expect(host.textContent).toContain('Claude')
    // And the workspace the assistant would be created in.
    expect(host.textContent).toContain('Studio')
    const shown = host.querySelector('[aria-label="Where the access is sent"]')
    expect(shown?.textContent).toBe('https://evil-looking.example')
    const consentCall = calls.find((c) => c.url.startsWith('/api/oauth/consent?'))
    expect(consentCall?.url).toBe(`/api/oauth/consent?request=${REQUEST}`)
    // The suggested scope is ticked, the rest is not.
    const boxes = [...host.querySelectorAll<HTMLInputElement>('input[type="checkbox"]')]
    expect(boxes.map((b) => b.checked)).toEqual([true, false])
    // The label defaults to the client's name AND where its codes go.
    const label = host.querySelector<HTMLInputElement>('input:not([type])')
    expect(label?.value).toBe('Claude via evil-looking.example')
  })

  it('approve posts the decision with the bearer and goes where the server says', async () => {
    const navigate = vi.fn()
    await render(navigate)
    await act(async () => {
      button('Approve').click()
    })
    await settle()
    const post = calls.find((c) => c.url === '/api/oauth/consent/approve')
    expect(post?.method).toBe('POST')
    expect(post?.auth).toBe('Bearer person-jwt')
    expect(post?.body).toEqual({
      request: REQUEST,
      label: 'Claude via evil-looking.example',
      scope: ['tasks:read'],
    })
    expect(navigate).toHaveBeenCalledWith(CODE_URL)
  })

  it('deny goes back to the client with access_denied', async () => {
    const navigate = vi.fn()
    await render(navigate)
    await act(async () => {
      button('Deny').click()
    })
    await settle()
    const post = calls.find((c) => c.url === '/api/oauth/consent/deny')
    expect(post?.method).toBe('POST')
    expect(post?.body).toEqual({ request: REQUEST })
    expect(navigate).toHaveBeenCalledWith(DENY_URL)
    expect(calls.some((c) => c.url === '/api/oauth/consent/approve')).toBe(false)
  })
})
