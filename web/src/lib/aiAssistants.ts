// The AI-assistants API, in one place.
//
// Two surfaces now read it: the workspace settings card that manages
// assistants, and the browser-extension page, which mints one and lists
// the ones it minted. The second was written after the first, and copying
// the fetch wrapper into it would have made the error handling -- the part
// that is easy to get subtly wrong and impossible to notice when it is --
// two implementations of one rule.
//
// Errors: the envelope reader lives in src/shared/errors.ts, because the
// shape is the SERVER's contract and the extension reads the same one.
// What is here is the transport and the endpoint names.

import { authFetch } from '../api/client'
import { errMessage } from '../api/client'

export type ScopeCategory = 'read' | 'write' | 'danger'

export type Scope = {
  key: string
  category: ScopeCategory
  label: string
  description: string
}

export type ConnectorInfo = { mcp_url: string; instructions_md: string }

export type Assistant = {
  id: string
  label: string
  provider: string | null
  model_id: string | null
  notes: string | null
  scope: string[]
  is_active: boolean
  version: number
  created_at: string
  updated_at: string
  token_prefix: string | null
}

/** Returned exactly once, at create or rotate. ``raw_secret`` is the only
 *  moment the value exists outside the server; nothing re-reads it. */
export type AssistantCreated = { assistant: Assistant; raw_secret: string }

export const CATEGORY_ORDER: readonly ScopeCategory[] = ['read', 'write', 'danger']

async function call<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await authFetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init.headers ?? {}) },
  })
  if (!res.ok) {
    // The server's own sentence when it sent one -- it is already
    // localized and it knows what was refused. errMessage falls back to
    // the catalogue only when there is nothing usable at all.
    let body: unknown
    try {
      body = await res.json()
    } catch {
      // No body, or not JSON: fall through to the status line, which is
      // all there is to say.
      body = null
    }
    throw new Error(body ? errMessage(body) : `HTTP ${res.status}`)
  }
  if (res.status === 204) return undefined as T
  return res.json() as Promise<T>
}

export const aiApi = {
  list: () => call<Assistant[]>('/ai-assistants'),
  connectorInfo: () => call<ConnectorInfo>('/ai-assistants/connector-info'),
  scopeCatalog: () => call<Scope[]>('/ai-assistants/scope-catalog'),
  create: (body: object) =>
    call<AssistantCreated>('/ai-assistants', { method: 'POST', body: JSON.stringify(body) }),
  update: (id: string, body: object) =>
    call<Assistant>(`/ai-assistants/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
  remove: (id: string) => call<void>(`/ai-assistants/${id}`, { method: 'DELETE' }),
  rotate: (id: string) => call<AssistantCreated>(`/ai-assistants/${id}/rotate`, { method: 'POST' }),
}
