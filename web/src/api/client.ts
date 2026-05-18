import createClient, { type Middleware } from 'openapi-fetch'
import type { paths } from './schema'
import {
  clearSession,
  getSession,
  isAdminMode,
  lastWorkspaceId,
  setSession,
} from '../auth/session'
import i18n from '../i18n'

// Authorization is the HTTPBearer scheme (injected here, not a typed
// param). Accept-Language drives the backend i18n catalog so error
// `detail` comes back localized. X-Workspace-Id is a typed per-call
// parameter (see workspaceHeader).
const authMiddleware: Middleware = {
  onRequest({ request }) {
    const s = getSession()
    if (s) request.headers.set('Authorization', `Bearer ${s.token}`)
    // Sent only while elevated; the server still re-checks the
    // capability, so this never escalates a non-admin.
    if (s && isAdminMode()) request.headers.set('X-Admin-Mode', '1')
    request.headers.set('Accept-Language', i18n.language)
    return request
  },
  onResponse({ response }) {
    // A revoked/expired token (auth.token_revoked / 401) drops the
    // session; RequireAuth then routes to /login.
    if (response.status === 401 && getSession()) clearSession()
    return response
  },
}

export const api = createClient<paths>({ baseUrl: '/api' })
api.use(authMiddleware)

/** Tenant scoping header required by workspace-scoped operations. */
export function workspaceHeader(): { 'x-workspace-id': string } {
  const s = getSession()
  if (!s) throw new Error('no session')
  return { 'x-workspace-id': s.workspaceId }
}

/** Backend domain error envelope ({code, detail}); see api/app.py. */
export type ApiError = { code?: string; detail?: string }

export function errCode(e: unknown): string | undefined {
  return (e as ApiError | undefined)?.code
}

export function errMessage(e: unknown): string {
  const v = e as ApiError | undefined
  return v?.detail ?? v?.code ?? i18n.t('error.generic')
}

// After auth we hold a token but no workspace context yet: fetch the
// user's workspaces and activate the remembered one (else the first).
// Switching later is in-app, no re-auth (ADR-0024).
export async function establishSession(token: string): Promise<void> {
  const { data, error } = await api.GET('/workspaces', {
    headers: { Authorization: `Bearer ${token}` },
  })
  if (error || !data || data.length === 0) {
    throw new Error(errMessage(error))
  }
  const remembered = lastWorkspaceId()
  const pick = data.find((w) => w.id === remembered) ?? data[0]
  setSession({ token, workspaceId: pick.id })
}
