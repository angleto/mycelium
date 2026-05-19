import createClient, { type Middleware } from 'openapi-fetch'
import type { paths } from './schema'
import {
  clearSession,
  getSession,
  getWorkspaceRole,
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
    // Effective workspace role ('' = default member). Server clamps it
    // to the real membership role, so a forged value cannot escalate.
    const wr = s ? getWorkspaceRole() : ''
    if (wr) request.headers.set('X-Workspace-Role', wr)
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

/** Raw authenticated fetch for binary endpoints (multipart upload,
 * file download) where the typed JSON client does not fit. Mirrors
 * authMiddleware exactly: same bearer, tenant, elevation and locale
 * headers, and the same 401 → clearSession behaviour, so it never
 * escalates and stays consistent with the typed client. Do NOT set
 * Content-Type for FormData bodies — the browser adds the multipart
 * boundary. `path` is relative to the `/api` base. */
export async function authFetch(
  path: string,
  init: RequestInit = {},
): Promise<Response> {
  const s = getSession()
  if (!s) throw new Error('no session')
  const h = new Headers(init.headers)
  h.set('Authorization', `Bearer ${s.token}`)
  h.set('x-workspace-id', s.workspaceId)
  if (isAdminMode()) h.set('X-Admin-Mode', '1')
  const wr = getWorkspaceRole()
  if (wr) h.set('X-Workspace-Role', wr)
  h.set('Accept-Language', i18n.language)
  const res = await fetch(`/api${path}`, { ...init, headers: h })
  if (res.status === 401 && getSession()) clearSession()
  return res
}

/** Backend domain error envelope ({code, detail}); see api/app.py.
 * `detail` is a string for our domain errors but a FastAPI 422 sends
 * an ARRAY of {loc,msg,type,...} validation objects — never render it
 * raw (it white-screens React: "Objects are not valid as a child"). */
export type ApiError = { code?: string; detail?: unknown }

export function errCode(e: unknown): string | undefined {
  return (e as ApiError | undefined)?.code
}

function validationLine(x: unknown): string | null {
  if (x && typeof x === 'object' && 'msg' in x) {
    const o = x as { msg?: unknown; loc?: unknown }
    const msg = typeof o.msg === 'string' ? o.msg : ''
    const loc = Array.isArray(o.loc)
      ? o.loc.filter((p) => p !== 'body').join('.')
      : ''
    return loc && msg ? `${loc}: ${msg}` : msg || null
  }
  return typeof x === 'string' ? x : null
}

// Always returns a string (a non-string detail must never reach JSX).
export function errMessage(e: unknown): string {
  const d = (e as ApiError | undefined)?.detail
  if (typeof d === 'string' && d) return d
  if (Array.isArray(d)) {
    const msgs = d.map(validationLine).filter((m): m is string => !!m)
    if (msgs.length) return msgs.join('; ')
  }
  if (d && typeof d === 'object') {
    const m = (d as { msg?: unknown }).msg
    if (typeof m === 'string' && m) return m
  }
  return (e as ApiError | undefined)?.code ?? i18n.t('error.generic')
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
