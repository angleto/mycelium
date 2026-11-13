import { api, workspaceHeader } from '../api/client'
import { findNotes } from '../api/notes'
import type { MentionKind } from './mentions'
import { isPrefixCandidate, lookupPrefix } from './prefixLookup'

// The two typeahead searches the editors share.
//
// They used to live inside RichEditor.tsx, next to the tiptap Suggestion
// plugins that were their only caller. The markdown source surface needs the
// same two searches for its own completion sources, and a second copy would
// be a second set of answers to "what can I link to from here" -- so they
// moved out. Nothing about them is rendering; they are data.

export type Cand = { kind: MentionKind; id: string; label: string }

export async function searchCandidates(query: string): Promise<Cand[]> {
  const h = workspaceHeader()
  const [tk, tg, nt] = await Promise.all([
    api.GET('/tasks', { params: { header: h } }),
    api.GET('/tags', { params: { header: h } }),
    noteCandidates(query),
  ])
  const q = query.trim().toLowerCase()
  const out: Cand[] = []
  for (const t of tk.data ?? []) {
    if (!q || t.title.toLowerCase().includes(q)) {
      out.push({ kind: 'task', id: t.id, label: t.title })
    }
  }
  for (const n of nt) out.push({ kind: 'note', id: n.id, label: n.label })
  for (const g of tg.data ?? []) {
    if (!q || g.name.toLowerCase().includes(q)) {
      out.push({ kind: 'tag', id: g.id, label: g.name })
    }
  }
  return out.slice(0, 8)
}

export type EntityCand = { kind: 'task' | 'note'; id: string; label: string }

export async function searchEntities(query: string): Promise<EntityCand[]> {
  const q = query.trim()
  const out: EntityCand[] = []
  const seen = new Set<string>()
  const take = (kind: 'task' | 'note', id: string, label: string) => {
    const key = `${kind}:${id}`
    if (seen.has(key)) return
    seen.add(key)
    out.push({ kind, id, label })
  }
  // A hex prefix resolves deterministically via /lookup, shown first.
  if (isPrefixCandidate(q)) {
    const res = await lookupPrefix(q.toLowerCase(), { kinds: ['task', 'note'] })
    for (const m of res?.matches ?? []) take(m.kind, m.id, m.title ?? m.id)
  }
  // Title substring over the workspace's tasks + notes (same instant
  // client-side filter the @-mention and the Cmd+K palette use).
  const h = workspaceHeader()
  const [tk, nt] = await Promise.all([
    api.GET('/tasks', { params: { header: h } }),
    noteCandidates(q),
  ])
  const lc = q.toLowerCase()
  for (const t of tk.data ?? []) {
    if (!lc || t.title.toLowerCase().includes(lc)) take('task', t.id, t.title)
  }
  for (const n of nt) take('note', n.id, n.label)
  return out.slice(0, 8)
}

/** Notes for a typeahead, asked of the server so an old note is findable:
 * the list's first page holds only the newest. The server matches the
 * title, any part and tag names; a title match is what a person typing a
 * name means, so those come first. */
async function noteCandidates(query: string): Promise<{ id: string; label: string }[]> {
  const lc = query.trim().toLowerCase()
  const rows = await findNotes(query, 20)
  const labelled = rows.map((n) => ({ id: n.id, label: n.title ?? n.kind }))
  const inTitle = labelled.filter((n) => !lc || n.label.toLowerCase().includes(lc))
  const elsewhere = labelled.filter((n) => lc && !n.label.toLowerCase().includes(lc))
  return [...inTitle, ...elsewhere]
}
