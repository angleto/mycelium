// Tiny REST helper for prod E2E. Token is the one baked into
// storageState; the helper reads it back from the storage file so
// specs can hit the API directly for fixture setup/teardown.
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))
const storage = JSON.parse(
  readFileSync(resolve(here, '.auth/storage.json'), 'utf8'),
) as {
  origins: { origin: string; localStorage: { name: string; value: string }[] }[]
}
const session = JSON.parse(
  storage.origins[0].localStorage.find((kv) => kv.name === 'flow.session')!
    .value,
) as { token: string; workspaceId: string }

export const TOKEN = session.token
export const WS_ID = session.workspaceId
export const BASE = 'https://flow.xeno.garden'
export const E2E_TAG_ID = 'c652a386-5505-484a-9b13-1407adc0af2c'

interface ReqOpts {
  method?: string
  body?: unknown
  editSession?: string
  // Retry 404 a few times. Prod shows a read-after-write gap between
  // fetch() connections (pool/replica routing); a freshly-created
  // task is invisible for ~200-1500ms to a second connection. Set
  // retry404 only for follow-ups that *should* find the row.
  retry404?: boolean
}
export async function req<T = unknown>(
  path: string,
  opts: ReqOpts = {},
): Promise<{ status: number; data: T | null }> {
  const headers: Record<string, string> = {
    Authorization: `Bearer ${TOKEN}`,
    'X-Workspace-Id': WS_ID,
    'Content-Type': 'application/json',
  }
  if (opts.editSession) headers['X-Edit-Session-Id'] = opts.editSession
  const send = async () =>
    fetch(`${BASE}/api${path}`, {
      method: opts.method ?? 'GET',
      headers,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    })
  let res = await send()
  if (opts.retry404 && res.status === 404) {
    for (const delay of [300, 600, 1200, 2000]) {
      await new Promise((r) => setTimeout(r, delay))
      res = await send()
      if (res.status !== 404) break
    }
  }
  let data: T | null = null
  try {
    data = (await res.json()) as T
  } catch {
    /* empty body */
  }
  return { status: res.status, data }
}

// Patch a task by fetching the freshest version first, so prod
// replica lag doesn't surface as a 409 against a stale
// expected_version. Returns the post-patch task.
export async function patchTaskWithFreshVersion<T = unknown>(
  taskId: string,
  body: Record<string, unknown>,
): Promise<{ status: number; data: T | null }> {
  for (let attempt = 0; attempt < 5; attempt++) {
    const cur = await req<{ version: number }>(`/tasks/${taskId}`, {
      retry404: true,
    })
    if (cur.status !== 200 || cur.data?.version === undefined) {
      return cur as { status: number; data: T | null }
    }
    const res = await req<T>(`/tasks/${taskId}`, {
      method: 'PATCH',
      body: { ...body, expected_version: cur.data.version },
      retry404: true,
    })
    if (res.status !== 409) return res
    await new Promise((r) => setTimeout(r, 400 * (attempt + 1)))
  }
  return { status: 409, data: null }
}
