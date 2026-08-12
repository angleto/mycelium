import { request as pwRequest, type APIRequestContext } from '@playwright/test'
import { E2E_EMAIL as EMAIL, E2E_PASSWORD as PASSWORD } from './global-setup'

// Direct API access for specs that need to SEED state (a task with a known
// description, a note part, an invoice) rather than click it into being.
//
// The base URL is configurable and defaults to the port CI uses. It used to
// be the literal ``http://localhost:8000``, repeated at five call sites, and
// that made every seeding spec silently undebuggable on a machine where
// 8000 belongs to another project: the request reaches the WRONG server (or
// hangs), and the spec dies of a 45s timeout with a blank screenshot and no
// hint that the port was the problem. Point MYCELIUM_E2E_API_URL at the API
// under test and the suite follows.
export const API_BASE = process.env.MYCELIUM_E2E_API_URL ?? 'http://localhost:8000'

export interface AuthedApi {
  ctx: APIRequestContext
  /** Authorization + workspace + JSON content type, ready to spread. */
  headers: Record<string, string>
  token: string
  workspaceId: string
}

/** Log in as the E2E admin and resolve their first workspace. The caller
 * owns the returned context and must ``dispose()`` it. */
export async function authedApi(
  extraHeaders: Record<string, string> = {},
): Promise<AuthedApi> {
  const ctx = await pwRequest.newContext({ baseURL: API_BASE })
  const auth = await (
    await ctx.post('/auth/login', { data: { email: EMAIL, password: PASSWORD } })
  ).json()
  const token = auth.token as string
  const ws = await (
    await ctx.get('/workspaces', { headers: { Authorization: `Bearer ${token}` } })
  ).json()
  const workspaceId = ws[0].id as string
  return {
    ctx,
    headers: {
      Authorization: `Bearer ${token}`,
      'X-Workspace-Id': workspaceId,
      'Content-Type': 'application/json',
      ...extraHeaders,
    },
    token,
    workspaceId,
  }
}
