import createClient, { type Middleware } from 'openapi-fetch'
import type { paths } from './schema'
import { clearSession, getSession } from '../auth/session'
import i18n from '../i18n'

// Authorization is the HTTPBearer security scheme (not an operation
// parameter), so it is injected here. Accept-Language drives the
// backend i18n catalog (docs/adr/0017) so error `detail` comes back
// localized. X-Org-Id is a typed per-call parameter (see orgHeader).
const authMiddleware: Middleware = {
  onRequest({ request }) {
    const s = getSession()
    if (s) request.headers.set('Authorization', `Bearer ${s.token}`)
    request.headers.set('Accept-Language', i18n.language)
    return request
  },
  onResponse({ response }) {
    if (response.status === 401) clearSession()
    return response
  },
}

export const api = createClient<paths>({ baseUrl: '/api' })
api.use(authMiddleware)

/** Tenant scoping header required by authenticated operations. */
export function orgHeader(): { 'x-org-id': string } {
  const s = getSession()
  if (!s) throw new Error('no session')
  return { 'x-org-id': s.orgId }
}

/** Backend domain error envelope ({code, detail}); see api/app.py. */
export type ApiError = { code?: string; detail?: string }

export function errMessage(e: unknown): string {
  const v = e as ApiError | undefined
  return v?.detail ?? v?.code ?? i18n.t('error.generic')
}
