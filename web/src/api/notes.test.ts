import { beforeEach, describe, expect, it, vi } from 'vitest'

// The list answers a page and a cursor (ADR-0068). These helpers are how
// every screen reads it, so what they promise is tested here, against a
// fake list of 450 notes, instead of in each of the screens.

const calls: Array<Record<string, unknown>> = []
let failAt: number | null = null

const ALL = Array.from({ length: 450 }, (_, i) => ({
  id: `00000000-0000-4000-8000-${String(i).padStart(12, '0')}`,
  title: `note ${i}`,
}))

vi.mock('./client', () => ({
  workspaceHeader: () => ({ 'x-workspace-id': 'ws' }),
  errMessage: (e: unknown) => String((e as { detail?: string })?.detail ?? e),
  api: {
    GET: async (_path: string, opts: { params: { query: Record<string, unknown> } }) => {
      const q = opts.params.query
      calls.push(q)
      if (failAt !== null && calls.length === failAt) {
        return { data: undefined, error: { detail: 'boom' } }
      }
      let rows = ALL
      if (Array.isArray(q.ids)) rows = rows.filter((r) => (q.ids as string[]).includes(r.id))
      const start = q.cursor ? Number(q.cursor) : 0
      const limit = Number(q.limit ?? 50)
      const items = rows.slice(start, start + limit)
      const next = start + limit < rows.length ? String(start + limit) : null
      return { data: { items, next_cursor: next, truncated: next !== null }, error: undefined }
    },
  },
}))

const { allNotes, findNotes, notesByIds, notesPage } = await import('./notes')

beforeEach(() => {
  calls.length = 0
  failAt = null
})

describe('notes helpers', () => {
  it('reads every page of the workspace by following the cursor', async () => {
    const rows = await allNotes()
    expect(rows).toHaveLength(450)
    expect(calls.map((c) => c.cursor ?? null)).toEqual([null, '200', '400'])
  })

  it('does not pass a failed page off as the end of the workspace', async () => {
    failAt = 2
    await expect(allNotes()).rejects.toThrow('boom')
  })

  it('degrades a failed single page to an empty last page', async () => {
    failAt = 1
    expect(await notesPage()).toEqual({ items: [], next_cursor: null, truncated: false })
  })

  it('reads notes by id in chunks the server accepts, archived included', async () => {
    const ids = [...ALL.slice(0, 250).map((r) => r.id), ALL[0].id]
    const rows = await notesByIds(ids)
    expect(rows).toHaveLength(250)
    expect(calls.map((c) => (c.ids as string[]).length)).toEqual([200, 50])
    expect(calls.every((c) => c.include_archived === true)).toBe(true)
  })

  it('asks for nothing when there is nothing to label', async () => {
    expect(await notesByIds([])).toEqual([])
    expect(calls).toHaveLength(0)
  })

  it('searches the server with the trimmed query and a small limit', async () => {
    await findNotes('  idea  ', 8)
    expect(calls[0]).toEqual({ q: 'idea', limit: 8 })
    await findNotes('   ')
    expect(calls[1]).toEqual({ limit: 20 })
  })
})
