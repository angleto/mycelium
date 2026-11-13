// Registering an OAuth client by hand (ADR-0069), against a fake HTTP edge.
//
// Pinned: the form sends the name and one redirect URI per non-blank line,
// the client id the server minted is shown to copy, nothing resembling a
// secret is asked for or shown, and removal goes to the right route.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { OAuthClientsSettings } from './OAuthClientsSettings'
import { parseRedirectUris } from '../lib/aiAssistants'
import { setSession } from '../auth/session'
import i18n from '../i18n'

type Call = { url: string; method: string; body: unknown }

let host: HTMLDivElement
let root: Root
let calls: Call[]
let rows: Array<Record<string, unknown>>

function json(body: unknown, status = 200): Response {
  return new Response(body === null ? null : JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

beforeEach(async () => {
  await i18n.changeLanguage('en')
  setSession({ token: 'person-jwt', workspaceId: 'ws-1' })
  calls = []
  rows = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = (init?.method ?? 'GET').toUpperCase()
      const body = init?.body ? JSON.parse(String(init.body)) : null
      calls.push({ url, method, body })
      if (url === '/api/oauth/clients' && method === 'GET') return json(rows)
      if (url === '/api/oauth/clients' && method === 'POST') {
        const row = {
          client_id: 'mycelium_pc_minted',
          client_name: body.client_name,
          redirect_uris: body.redirect_uris,
          created_at: '2026-11-13T10:00:00Z',
          created_by_id: 'u1',
        }
        rows = [row]
        return json(row, 201)
      }
      if (url.startsWith('/api/oauth/clients/') && method === 'DELETE') {
        rows = []
        return new Response(null, { status: 204 })
      }
      return json({}, 404)
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
  vi.restoreAllMocks()
})

async function settle() {
  for (let i = 0; i < 5; i++) {
    await act(async () => {
      await Promise.resolve()
    })
  }
}

function setValue(el: HTMLInputElement | HTMLTextAreaElement, value: string) {
  const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement : HTMLInputElement
  Object.getOwnPropertyDescriptor(proto.prototype, 'value')?.set?.call(el, value)
  el.dispatchEvent(new Event('input', { bubbles: true }))
}

describe('parseRedirectUris', () => {
  it('takes one URI per line and drops blanks', () => {
    expect(parseRedirectUris(' https://a.example/cb \n\n http://127.0.0.1:9/cb\n')).toEqual([
      'https://a.example/cb',
      'http://127.0.0.1:9/cb',
    ])
  })
})

describe('OAuthClientsSettings', () => {
  it('registers a client and shows the id to copy, with no secret', async () => {
    await act(async () => {
      root.render(<OAuthClientsSettings />)
    })
    await settle()
    const name = host.querySelector('input') as HTMLInputElement
    const uris = host.querySelector('textarea') as HTMLTextAreaElement
    await act(async () => {
      setValue(name, 'My Cursor')
      setValue(uris, 'http://127.0.0.1:7777/cb\nhttps://app.example/cb')
    })
    const submit = [...host.querySelectorAll('button')].find(
      (b) => b.textContent === 'Register client',
    ) as HTMLButtonElement
    await act(async () => {
      submit.click()
    })
    await settle()
    const post = calls.find((c) => c.method === 'POST')
    expect(post?.body).toEqual({
      client_name: 'My Cursor',
      redirect_uris: ['http://127.0.0.1:7777/cb', 'https://app.example/cb'],
    })
    expect(host.textContent).toContain('mycelium_pc_minted')
    expect(host.textContent).toContain('OAuth client ID')
    expect(host.querySelector('input[type="password"]')).toBeNull()
  })

  it('removes a client through the delete route', async () => {
    rows = [
      {
        client_id: 'mycelium_pc_old',
        client_name: 'Old',
        redirect_uris: ['https://a.example/cb'],
        created_at: '2026-11-13T10:00:00Z',
        created_by_id: 'u1',
      },
    ]
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    await act(async () => {
      root.render(<OAuthClientsSettings />)
    })
    await settle()
    const remove = [...host.querySelectorAll('button')].find(
      (b) => b.textContent === 'Remove',
    ) as HTMLButtonElement
    await act(async () => {
      remove.click()
    })
    await settle()
    expect(calls.some((c) => c.method === 'DELETE' && c.url === '/api/oauth/clients/mycelium_pc_old')).toBe(
      true,
    )
    expect(host.textContent).toContain('No clients registered by hand')
  })
})
