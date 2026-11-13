// Settings -> Connections (ADR-0069), against a fake HTTP edge.
//
// Pinned: the tab says which workspace its lists belong to, shows the one
// URL to paste (``<origin>/mcp/``) with the ways a host can identify itself,
// and carries both lists that moved here from the Workspace tab -- the AI
// assistants and the OAuth clients registered by hand.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { SettingsConnectionsRoute } from './SettingsConnectionsRoute'
import { mcpServerUrl } from '../lib/aiAssistants'
import { setSession } from '../auth/session'
import i18n from '../i18n'

// The workspace snapshot comes from the shared store, which fetches through
// the typed client bound to the real fetch at import; stubbed with what it
// would hold.
vi.mock('../auth/useMyWorkspace', () => ({
  useMyWorkspace: () => ({ ws: { name: 'Studio' }, reload: async () => {} }),
}))

let host: HTMLDivElement
let root: Root

beforeEach(async () => {
  await i18n.changeLanguage('en')
  setSession({ token: 'person-jwt', workspaceId: 'ws-1' })
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      const body =
        url === '/api/ai-assistants/connector-info'
          ? { mcp_url: '/mcp', instructions_md: 'paste' }
          : []
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

describe('mcpServerUrl', () => {
  it('is the origin plus /mcp/, whatever the origin ends with', () => {
    expect(mcpServerUrl('https://m.example')).toBe('https://m.example/mcp/')
    expect(mcpServerUrl('https://m.example/')).toBe('https://m.example/mcp/')
  })
})

describe('SettingsConnectionsRoute', () => {
  it('names the workspace, shows the URL and both moved lists', async () => {
    await act(async () => {
      root.render(
        <MemoryRouter>
          <SettingsConnectionsRoute />
        </MemoryRouter>,
      )
    })
    for (let i = 0; i < 5; i++) {
      await act(async () => {
        await Promise.resolve()
      })
    }
    const text = host.textContent ?? ''
    expect(text).toContain('Workspace: Studio')
    const url = host.querySelector('[aria-label="MCP server URL"]')
    expect(url?.textContent).toBe(`${window.location.origin}/mcp/`)
    expect(text).toContain('Automatic registration')
    expect(text).toContain('Client metadata document')
    expect(text).toContain('OAuth client ID')
    // The two lists that moved here from Settings -> Workspace.
    expect(text).toContain('AI assistants')
    expect(text).toContain('OAuth clients registered by hand')
  })
})
