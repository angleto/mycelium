import createClient, { type Middleware } from 'openapi-fetch'
import type { components, paths } from './schema'
import {
  clearSession,
  getSession,
  getWorkspaceRole,
  isAdminMode,
  lastWorkspaceId,
  setSession,
  updateSessionTokens,
} from '../auth/session'
import i18n from '../i18n'
import { initialWorkspaceId } from '../lib/workspaceChoice'

// Single-flight refresh promise: many in-flight requests may all
// 401 at once when the access JWT expires; without coalescing, each
// would race to /auth/refresh and the second arrival would replay a
// now-used refresh row (theft signal → family revoked → all logged
// out). Holding one promise per refresh attempt collapses the storm
// into one rotation and shares the result.
let inflightRefresh: Promise<string | null> | null = null

/** Rotate the access (+ refresh) token via /auth/refresh. Returns
 * the new access token (or null if refresh failed / no refresh token
 * is held, in which case the caller drops the session). The promise
 * is cached for the lifetime of the rotation so concurrent 401s
 * collapse to one server roundtrip. */
async function refreshAccessToken(): Promise<string | null> {
  if (inflightRefresh) return inflightRefresh
  const s = getSession()
  if (!s || !s.refreshToken) return null
  inflightRefresh = (async () => {
    try {
      const res = await fetch('/api/auth/refresh', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ refresh_token: s.refreshToken }),
      })
      if (!res.ok) return null
      const data = (await res.json()) as {
        token: string
        refresh_token: string
      }
      updateSessionTokens(data.token, data.refresh_token)
      return data.token
    } catch {
      return null
    } finally {
      inflightRefresh = null
    }
  })()
  return inflightRefresh
}

function applyAuthHeaders(headers: Headers, token: string): void {
  headers.set('Authorization', `Bearer ${token}`)
  if (isAdminMode()) headers.set('X-Admin-Mode', '1')
  const wr = getWorkspaceRole()
  if (wr) headers.set('X-Workspace-Role', wr)
  headers.set('Accept-Language', i18n.language)
}

// Authorization is the HTTPBearer scheme (injected here, not a typed
// param). Accept-Language drives the backend i18n catalog so error
// `detail` comes back localized. X-Workspace-Id is a typed per-call
// parameter (see workspaceHeader).
//
// On 401 the middleware tries one refresh and replays the original
// request transparently; only if refresh fails do we drop the
// session and let RequireAuth route to /login. The refresh path
// itself is /auth/refresh (NoAuthMiddleware) so it can't loop.
const authMiddleware: Middleware = {
  onRequest({ request }) {
    const s = getSession()
    if (s) request.headers.set('Authorization', `Bearer ${s.token}`)
    if (s && isAdminMode()) request.headers.set('X-Admin-Mode', '1')
    const wr = s ? getWorkspaceRole() : ''
    if (wr) request.headers.set('X-Workspace-Role', wr)
    request.headers.set('Accept-Language', i18n.language)
    return request
  },
  async onResponse({ request, response }) {
    if (response.status !== 401 || !getSession()) return response
    // Avoid recursion: a 401 on /auth/refresh itself means the
    // refresh token is dead — drop session immediately.
    if (new URL(request.url).pathname.endsWith('/auth/refresh')) {
      clearSession()
      return response
    }
    const fresh = await refreshAccessToken()
    if (!fresh) {
      clearSession()
      return response
    }
    const retryHeaders = new Headers(request.headers)
    applyAuthHeaders(retryHeaders, fresh)
    const retry = await fetch(request.url, {
      method: request.method,
      headers: retryHeaders,
      body: request.body,
      // openapi-fetch reads the body once; clone the request just in
      // case the original was already consumed.
      // (Request bodies that are JSON have already been serialized
      // into request.body, which is a stream the platform supports
      // replaying via fetch for non-streamed bodies.)
    })
    return retry
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
  applyAuthHeaders(h, s.token)
  h.set('x-workspace-id', s.workspaceId)
  const res = await fetch(`/api${path}`, { ...init, headers: h })
  if (res.status !== 401 || !getSession()) return res
  // Same refresh-and-retry contract as the typed client.
  const fresh = await refreshAccessToken()
  if (!fresh) {
    clearSession()
    return res
  }
  const retryHeaders = new Headers(init.headers)
  applyAuthHeaders(retryHeaders, fresh)
  retryHeaders.set('x-workspace-id', s.workspaceId)
  return fetch(`/api${path}`, { ...init, headers: retryHeaders })
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

/** Unified server-side search (tasks + memory blobs). Returns the
 * subset of TASK ids that match the query, hits for ``kind='task'``
 * only (the SPA composes them with its structured client-side filters
 * via id-set intersection). The endpoint itself is wider and returns
 * both kinds + snippets; this helper is the narrow consumer that
 * TasksRoute needs. Lives here (not in api/search.ts) until the schema
 * is regenerated -- the typed client doesn't expose /search yet.
 *
 * Aborts on the next call automatically via the caller's
 * AbortController (passed in init.signal); a debounce + abort pattern in
 * the route keeps fast typing from racing the network. */
export type ServerSearchHit = {
  kind: string
  task_id: string | null
  note_id: string | null
  part_id: string | null
  blob_id: string
  title: string | null
  snippet: string | null
  score: number
}

/** Fire-and-forget search-click telemetry (ADR-0035 recall_at_k):
 * which query led the user to open which entity, at which 1-based rank
 * of the ranked /search result list, out of how many ranked hits. The
 * nightly garden-health snapshot aggregates these into the recall
 * sensor. Errors are swallowed: telemetry must never break (or even
 * delay) navigation. */
export function logSearchClick(ev: {
  q: string
  hitKind: 'task' | 'note' | 'blob'
  hitId: string
  rank: number
  resultCount: number
}): void {
  void authFetch('/search/click', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      q: ev.q,
      hit_kind: ev.hitKind,
      hit_id: ev.hitId,
      rank: ev.rank,
      result_count: ev.resultCount,
    }),
  }).catch(() => {
    /* telemetry only */
  })
}

export async function searchTasksByText(
  query: string,
  signal?: AbortSignal,
  kinds: string[] = ['task'],
): Promise<ServerSearchHit[]> {
  const res = await authFetch('/search', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      q: query,
      kinds,
      limit: 100,
      operation_id: 'tasks-route-search',
    }),
    signal,
  })
  if (!res.ok) {
    // 422 (empty q) and 401 propagate; the caller treats anything
    // non-2xx as "fall back to client-side filter only".
    return []
  }
  const data = (await res.json()) as ServerSearchHit[]
  return data
}

/** Server-side note search over the WHOLE corpus (not just the newest
 * window the plain list returns): ``GET /notes?q=`` filters by note
 * title, part body and tag name. ``tagId`` ANDs the active tag filter
 * so search composes with it. Typed via the shared client (auth +
 * X-Workspace-Id + 401-refresh handled by the middleware); ``data ?? []``
 * so a non-2xx (or a request superseded mid-type) degrades to the
 * caller's client-side filter over the already-loaded notes. */
export async function searchNotesByText(
  query: string,
  tagId?: string,
  signal?: AbortSignal,
): Promise<components['schemas']['NoteListOut'][]> {
  const { data } = await api.GET('/notes', {
    params: {
      header: workspaceHeader(),
      query: { q: query, ...(tagId ? { tag_id: tagId } : {}) },
    },
    signal,
  })
  return data ?? []
}

// After auth we hold a token but no workspace context yet: fetch the
// user's workspaces and activate the remembered one (else the first).
// Switching later is in-app, no re-auth (ADR-0024). ``refreshToken``
// is the long-lived rotating credential returned by
// login/signup/verify-email/refresh; if absent the SPA falls back to
// the legacy "401 → /login" behaviour.
export async function establishSession(
  token: string,
  refreshToken?: string,
): Promise<void> {
  const { data, error } = await api.GET('/workspaces', {
    headers: { Authorization: `Bearer ${token}` },
  })
  if (error || !data || data.length === 0) {
    throw new Error(errMessage(error))
  }
  // Never land in an archived workspace: the list is name-ordered and
  // includes archived rows, so the old ``data[0]`` fallback dropped a
  // fresh browser straight into one whenever it sorted first.
  const pick = initialWorkspaceId(data, lastWorkspaceId())
  if (!pick) throw new Error(errMessage(error))
  setSession({ token, workspaceId: pick, refreshToken })
}
