// Reading the notes list, one page at a time (ADR-0068).
//
// ``GET /notes`` answers a page and a cursor. It used to answer the 200
// newest notes in one go, which every screen took as "all the notes": a
// picker filtered titles it never saw past the 200th, the bin missed every
// note deleted before the newest 200, the garden map left nodes out. Each
// helper below matches one way a screen uses the list, so a screen asks for
// what it shows.
import { api, errMessage, workspaceHeader } from './client'
import type { components, operations } from '../shared/schema'

export type NoteRow = components['schemas']['NoteListOut']
export type NotesPage = components['schemas']['NotesPage']
export type NotesQuery = NonNullable<operations['list_notes_notes_get']['parameters']['query']>

/** The server's page-size ceiling, and the most ids one request may name. */
export const PAGE_MAX = 200

/** One page. A failed request is an empty last page, which is how every
 * caller already degraded when the list did not load. */
export async function notesPage(query: NotesQuery = {}, signal?: AbortSignal): Promise<NotesPage> {
  const { data } = await api.GET('/notes', {
    params: { header: workspaceHeader(), query },
    signal,
  })
  return data ?? { items: [], next_cursor: null, truncated: false }
}

/** Every page, following the cursor. Only for a view that genuinely shows
 * the whole workspace (the garden map); a list a person scrolls asks for
 * pages with ``notesPage``. Throws on a failed page. */
export async function allNotes(query: NotesQuery = {}): Promise<NoteRow[]> {
  const out: NoteRow[] = []
  let cursor: string | null = null
  do {
    const page = await strictPage({ ...query, limit: PAGE_MAX, cursor })
    out.push(...page.items)
    cursor = page.next_cursor ?? null
  } while (cursor)
  return out
}

/** One page, or an error: a view of the whole workspace must not pass a
 * failed page off as the end of the list. */
async function strictPage(query: NotesQuery): Promise<NotesPage> {
  const { data, error } = await api.GET('/notes', {
    params: { header: workspaceHeader(), query },
  })
  if (error || !data) throw new Error(errMessage(error))
  return data
}

/** The given notes, whatever their age, archived ones included: what a
 * panel needs to label notes it already knows by id. Deleted notes are
 * left out, as a link to one is not shown. */
export async function notesByIds(ids: string[]): Promise<NoteRow[]> {
  const unique = [...new Set(ids)]
  const out: NoteRow[] = []
  for (let i = 0; i < unique.length; i += PAGE_MAX) {
    const chunk = unique.slice(i, i + PAGE_MAX)
    const page = await notesPage({ ids: chunk, include_archived: true, limit: PAGE_MAX })
    out.push(...page.items)
  }
  return out
}

/** Notes matching ``q`` server-side (title, any part, a tag name), newest
 * first, at most ``limit``: a picker's candidates as the person types.
 * An empty query returns the newest notes. */
export async function findNotes(
  q: string,
  limit = 20,
  signal?: AbortSignal,
): Promise<NoteRow[]> {
  const needle = q.trim()
  const page = await notesPage({ ...(needle ? { q: needle } : {}), limit }, signal)
  return page.items
}
