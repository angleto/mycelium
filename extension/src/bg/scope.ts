// Which workspace the panel is looking at, and how narrowly.
//
// The focus is ONE tag id, never a list. Every task carries both its
// client tag and its project tag, and the server's tag filter is a
// faceted AND -- an entity must carry every requested tag -- so sending a
// client together with its projects matches nothing at all. The app's own
// focus state expands to a list for a different endpoint that matches by
// OR; copying that shape here would produce an empty panel and no error.

import { call } from './api'
import { storage } from './storage'
import type { Result, ScopeSel } from '../shared/protocol'
import type { StoredConnection } from './storage'

export async function readScope(): Promise<ScopeSel> {
  const workspaceId = await storage.activeWorkspace()
  if (!workspaceId) return { workspaceId: null, focus: null }
  return { workspaceId, focus: await storage.scope(workspaceId) }
}

export async function writeScope(next: ScopeSel): Promise<ScopeSel> {
  await storage.setActiveWorkspace(next.workspaceId)
  if (next.workspaceId) await storage.setScope(next.workspaceId, next.focus)
  return readScope()
}

interface TagRow {
  id: string
  name: string
}

/** Clients, through the slim tag route rather than /clients: the client
 *  record is thirty-odd fields of invoicing detail, and a picker needs an
 *  id and a name. */
export async function findClients(
  conn: StoredConnection,
  q: string,
): Promise<Result<TagRow[]>> {
  const res = await call<TagRow[]>(conn, '/tags', {
    query: { kind: 'client', q: q || undefined, limit: '8', recent: q ? undefined : 'true' },
  })
  if (!res.ok) return res
  return { ok: true, data: res.data.map((t) => ({ id: t.id, name: t.name })) }
}

interface ProjectRow {
  id: string
  name: string
  client_tag_id: string
}

export async function findProjects(
  conn: StoredConnection,
  q: string,
  clientTagId?: string,
): Promise<Result<{ id: string; name: string; clientTagId: string }[]>> {
  const res = await call<ProjectRow[]>(conn, '/projects', {
    query: {
      q: q || undefined,
      limit: '8',
      recent: q ? undefined : 'true',
      client_tag_id: clientTagId,
    },
  })
  if (!res.ok) return res
  return {
    ok: true,
    data: res.data.map((p) => ({ id: p.id, name: p.name, clientTagId: p.client_tag_id })),
  }
}
