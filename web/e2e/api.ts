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
  const ws = (await (
    await ctx.get('/workspaces', { headers: { Authorization: `Bearer ${token}` } })
  ).json()) as { id: string; name: string; status: string }[]
  // The workspace the APP will be in, not merely the first row.
  //
  // This used to be `ws[0]`, which silently agreed with the SPA only
  // because the SPA also took the first row. It no longer does: landing
  // a fresh login inside an ARCHIVED workspace was a bug, so
  // `establishSession` now prefers the first ACTIVE one (see
  // src/lib/workspaceChoice.ts, `initialWorkspaceId`). The rule is
  // mirrored rather than imported because the e2e project does not
  // compile src/.
  //
  // Get this wrong and a spec seeds a task, an invoice or a client into
  // a workspace the browser is not in: nothing errors, the seeded row
  // is simply never rendered, and the spec dies of a timeout on a page
  // that looks fine. Archived leftovers accumulate in a long-lived e2e
  // database (each focus-isolation run archives two), and they sort
  // before "Personal".
  const home = ws.find((w) => w.status !== 'archived') ?? ws[0]
  const workspaceId = home.id
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
