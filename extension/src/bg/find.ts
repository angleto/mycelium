// Finding a task or a note.
//
// Three branches, and the order they render in is the design:
//
//   1. the CODE branch, when the query looks like an entity code. It is
//      deterministic and cannot be slow -- no embedding, no stemming --
//      so it always paints first. A person who typed an id is not
//      searching, they are opening;
//   2. the RANKED branch, hybrid text plus semantic, fused server-side.
//      Its semantic leg is time-boxed by the server and degrades to text
//      matches, and when that happens the panel says so rather than
//      presenting a thinner answer as the whole answer;
//   3. RECENTS, when there is no query at all.
//
// What this deliberately does NOT do is hold a copy of the workspace to
// filter locally. The app's palette fetches every task AND every note
// each time it opens; that is affordable inside a page that already
// loaded them and is not affordable here, where the panel must paint in
// the time it takes to press a key.

import { RESOLVE_ID, lookupPath, searchClickBody, SEARCH_CLICK_PATH, withRecent } from '@shared'
import type { LookupOut, RecentItem } from '@shared'
import { call } from './api'
import { entityRoute } from './config'
import { parseQuery } from '../shared/query'
import { findClients, findProjects } from './scope'
import { storage } from './storage'
import type { EntityRow, FindResult, Result, ScopeSel } from '../shared/protocol'
import type { StoredConnection } from './storage'

const HEX8 = /^[0-9a-f]{4,36}$/i

export function code(id: string): string {
  return id.slice(0, 8)
}

interface SearchHit {
  kind: string
  task_id: string | null
  note_id: string | null
  title: string | null
  snippet: string | null
  tags?: { id: string; kind: string; name: string }[]
}

/** A stored recents row carries only what the shared contract defines;
 *  the code is derived rather than stored, so a change to how a code is
 *  formed does not leave old rows showing the old shape. */
function rowFromRecent(item: RecentItem): EntityRow {
  return {
    kind: item.kind,
    id: item.id,
    title: item.title,
    code: code(item.id),
    route: item.route,
  }
}

function rowFromHit(hit: SearchHit, rank: number): EntityRow | null {
  const kind = hit.kind === 'task' ? 'task' : hit.kind === 'note' ? 'note' : null
  if (!kind) return null
  const id = kind === 'task' ? hit.task_id : hit.note_id
  if (!id) return null
  const project = hit.tags?.find((t) => t.kind === 'project')?.name ?? null
  return {
    kind,
    id,
    title: hit.title ?? code(id),
    code: code(id),
    route: entityRoute(kind, code(id)),
    snippet: hit.snippet,
    projectName: project,
    rank,
  }
}

/** Turn `in:<text>` into the tag it names. Clients first, then
 *  projects: a person typing a client's name means the client, and the
 *  two namespaces do overlap in practice ("Acme" the client and "Acme"
 *  the project). `*` is everything, which is the way OUT of a pinned
 *  focus without touching the pinned selection. */
async function resolveScopeOverride(
  conn: StoredConnection,
  needle: string,
): Promise<ScopeSel['focus'] | 'all' | null> {
  const wanted = needle.trim()
  if (!wanted) return null
  if (wanted === '*') return 'all'

  const clients = await findClients(conn, wanted)
  if (clients.ok && clients.data.length > 0) {
    const hit = clients.data[0]
    if (hit) return { tagId: hit.id, kind: 'client', name: hit.name }
  }
  const projects = await findProjects(conn, wanted)
  if (projects.ok && projects.data.length > 0) {
    const hit = projects.data[0]
    if (hit) return { tagId: hit.id, kind: 'project', name: hit.name }
  }
  return null
}

/** `@name` is a tag on both surfaces, and the server takes ids. One
 *  bounded lookup per name rather than a local index: the panel must not
 *  hold a copy of the workspace to answer a question the server answers
 *  in one call. */
async function resolveTags(conn: StoredConnection, names: string[]): Promise<string[]> {
  const ids: string[] = []
  // Three is generous for a query line and bounds the fan-out. The
  // filter is an AND, so a fourth tag would in any case be narrowing
  // something already narrow.
  for (const name of names.slice(0, 3)) {
    const res = await call<{ id: string; name: string }[]>(conn, '/tags', {
      query: { q: name, limit: '5' },
    })
    if (!res.ok) continue
    const exact = res.data.find((t) => t.name.toLowerCase() === name.toLowerCase())
    const chosen = exact ?? res.data[0]
    if (chosen) ids.push(chosen.id)
  }
  return ids
}

export async function query(
  conn: StoredConnection,
  scope: ScopeSel,
  q: string,
  signal?: AbortSignal,
): Promise<Result<FindResult>> {
  const parsed = parseQuery(q)
  const needle = parsed.text.trim()
  if (!needle && !parsed.tags.length && !parsed.scope) {
    const recent = await storage.recents(conn.workspaceId)
    return { ok: true, data: { rows: recent.map(rowFromRecent), rankedCount: 0, degraded: false } }
  }

  const rows: EntityRow[] = []
  const seen = new Set<string>()

  if (HEX8.test(needle)) {
    // include_archived, always: the archive shelf must not hide an
    // entity from its own identifier. Resolving an id asks "what IS
    // this", and the answer cannot depend on where it was filed.
    const res = await call<LookupOut>(
      conn,
      lookupPath(needle, { ...RESOLVE_ID, kinds: ['task', 'note'] }),
      { signal },
    )
    if (res.ok) {
      for (const match of res.data.matches) {
        const row: EntityRow = {
          kind: match.kind,
          id: match.id,
          title: match.title ?? code(match.id),
          code: code(match.id),
          route: entityRoute(match.kind, code(match.id)),
          state: match.state_name,
          isArchived: match.is_archived,
        }
        if (!seen.has(row.route)) {
          seen.add(row.route)
          rows.push(row)
        }
      }
    }
  }

  // An inline `in:` overrides the pinned selection for THIS query only:
  // it never writes the persistent scope, so a person can look outside
  // their focus without losing it.
  const override = parsed.scope ? await resolveScopeOverride(conn, parsed.scope.needle) : null
  const effective = override === 'all' ? null : (override ?? scope.focus)
  const focusTag = effective?.tagId
  const tagIds = [...(focusTag ? [focusTag] : []), ...(await resolveTags(conn, parsed.tags))]

  const ranked = await call<SearchHit[]>(conn, '/search', {
    method: 'POST',
    body: {
      // The focus contributes ONE tag id. See scope.ts: the server's tag
      // filter is an AND, so a client plus its projects matches nothing.
      // Explicit @tags are ANDed on top, which is what they mean here and
      // on /tasks alike.
      q: needle || parsed.tags.join(' '),
      kinds: parsed.kinds,
      tag_ids: tagIds,
      include_archived: parsed.includeArchived,
      limit: 20,
      operation_id: 'ext-find',
    },
    // A project focus must ALSO scope note retrieval, which the server
    // narrows by project rather than by tag. A client focus cannot: a
    // client spans several projects and note hits take one. The panel
    // says so instead of quietly under-returning.
    projectId: effective?.kind === 'project' ? focusTag : undefined,
    signal,
  })

  if (!ranked.ok) {
    // The code branch may already have an answer worth showing. A failed
    // ranked search on top of a resolved id is a notice, not a blank.
    if (rows.length) return { ok: true, data: { rows, rankedCount: 0, degraded: true } }
    return ranked
  }

  let rank = 0
  for (const hit of ranked.data) {
    rank += 1
    const row = rowFromHit(hit, rank)
    if (!row || seen.has(row.route)) continue
    seen.add(row.route)
    rows.push(row)
  }

  return {
    ok: true,
    data: {
      rows,
      rankedCount: ranked.data.length,
      degraded: false,
      // Atoms the panel could not honour. Shown rather than dropped in
      // silence: the query that ran is not the query that was typed, and
      // the person is the only one who can tell whether that matters.
      unresolved: parsed.unresolved,
      scope: effective,
    },
  }
}

/** Record an open: the recents list, and -- only for a ranked row -- the
 *  server's recall telemetry. A recents row and a code resolution have no
 *  rank, and reporting a fabricated one would poison the sensor rather
 *  than merely miss a data point. */
export async function opened(
  conn: StoredConnection,
  row: EntityRow,
  q: string,
  rankedCount: number,
): Promise<void> {
  const item: RecentItem = { kind: row.kind, id: row.id, title: row.title, route: row.route }
  const before = await storage.recents(conn.workspaceId)
  await storage.setRecents(conn.workspaceId, withRecent(before, item))

  if (row.rank === undefined) return
  void call(conn, SEARCH_CLICK_PATH, {
    method: 'POST',
    body: searchClickBody({
      q,
      hitKind: row.kind,
      hitId: row.id,
      rank: row.rank,
      resultCount: rankedCount,
    }),
  })
}
