import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { NoteQuickActions } from './NoteQuickActions'
import { notesPage, PAGE_MAX, type NotesQuery } from '../api/notes'
import { useNotePins } from '../api/pins'
import { usePersistedFlag } from '../lib/persistedFlag'
import type { components } from '../shared'

type Note = components['schemas']['NoteListOut']

// Persisted UI state, like the recent tasks: whether the person left the
// panel open, and how many rows they want.
const OPEN_KEY = 'mycelium.notes.recent.open'
const COUNT_KEY = 'mycelium.notes.recent.count'
const DEFAULT_COUNT = 4
const MIN_COUNT = 1
const MAX_COUNT = 20

function readCount(): number {
  try {
    const v = Number(localStorage.getItem(COUNT_KEY))
    if (Number.isFinite(v) && v >= MIN_COUNT && v <= MAX_COUNT) return Math.floor(v)
  } catch {
    /* ignore */
  }
  return DEFAULT_COUNT
}

// The last N notes worked on, most recent first, above the notes list: the
// way straight back to what the person just touched. Recent here is the
// garden's liveness clock (``order_by=touched_at``): the last time a person
// or an agent edited the note or any of its parts, else its creation. Not
// ``updated_at``, the clock the recent tasks read: on a note that one is the
// row-write clock, which an edit made inside a part never moves (so a note
// worked on every day through its parts would never show up here) and which
// a machine's own write does. A pin moves neither.
//
// Unlike the recent tasks it asks the server rather than sorting the rows
// the page already holds: the notes list is paged by creation, so the page
// in hand is the newest-created fifty, and a note written long ago and
// edited today would never be among them. The query carries the page's tag
// filter and focus, so the panel narrows with the list below it.
export function RecentNotes({
  query,
  refreshKey,
}: {
  query: Pick<NotesQuery, 'tag_id' | 'any_tag_ids'>
  /** Changes whenever the page's own list reloads, so this one follows. */
  refreshKey?: unknown
}) {
  const { t } = useTranslation()
  // Closed on a profile that has never chosen, like the recent tasks: open
  // it pushes the list the person came for further down. Whoever opens it
  // keeps it open.
  const [open, setOpen] = usePersistedFlag(OPEN_KEY, false)
  const [count, setCount] = useState(readCount)
  const [rows, setRows] = useState<Note[] | null>(null)
  const { ids: pinnedIds } = useNotePins()

  useEffect(() => {
    try {
      localStorage.setItem(COUNT_KEY, String(count))
    } catch {
      /* ignore */
    }
  }, [count])

  // Enough rows that the pinned ones can be dropped and ``count`` remain.
  const want = Math.min(PAGE_MAX, count + pinnedIds.length)
  const tagId = query.tag_id
  const anyTags = (query.any_tag_ids ?? []).join(',')
  useEffect(() => {
    if (!open) return
    const ctl = new AbortController()
    void (async () => {
      const page = await notesPage(
        {
          ...(tagId ? { tag_id: tagId } : {}),
          ...(anyTags ? { any_tag_ids: anyTags.split(',') } : {}),
          order_by: 'touched_at',
          order_desc: true,
          limit: want,
        },
        ctl.signal,
      )
      if (!ctl.signal.aborted) setRows(page.items)
    })()
    return () => ctl.abort()
  }, [open, want, tagId, anyTags, refreshKey])

  // A pinned note is already in the list above this one: shown here too it
  // only takes one of the few rows from a note that is not at hand. Filtered
  // before the cut, so the count is still that many recent notes.
  const recent = (rows ?? []).filter((n) => !pinnedIds.includes(n.id)).slice(0, count)

  return (
    <section className="recentwidget">
      <div className="recentwidget__head">
        <button
          type="button"
          className="recentwidget__toggle"
          aria-expanded={open}
          onClick={() => setOpen(!open)}
        >
          <span className="recentwidget__caret" aria-hidden="true">
            {open ? '▾' : '▸'}
          </span>
          {t('notes.recentTitle')}
        </button>
        {open && (
          <label className="recentwidget__count">
            {t('notes.recentCount')}
            <input
              type="number"
              min={MIN_COUNT}
              max={MAX_COUNT}
              value={count}
              onChange={(e) => {
                const n = Number(e.target.value)
                if (Number.isFinite(n)) {
                  setCount(Math.min(MAX_COUNT, Math.max(MIN_COUNT, Math.floor(n))))
                }
              }}
            />
          </label>
        )}
      </div>

      {open &&
        rows !== null &&
        (recent.length === 0 ? (
          <p className="hint">{t('notes.none')}</p>
        ) : (
          <ul className="list recentlist">
            {recent.map((n) => (
              <NoteShortRow key={n.id} note={n} />
            ))}
          </ul>
        ))}
    </section>
  )
}

/** One note as a short row: the title on its line, the kind beside it, the
 *  quick actions on the line below. No time: the one the row carries is
 *  ``updated_at``, which is not the clock this panel orders by, and a
 *  "3 weeks ago" on the note at the top of Recent would contradict it. The recent notes and the
 *  pinned ones are drawn with the same row, so the two lists read the same
 *  and a change to one is a change to both. */
export function NoteShortRow({ note: n }: { note: Note }) {
  const { t } = useTranslation()
  const title = n.title || t('notes.untitled')
  return (
    <li className="recentrow">
      <div className="recentrow__line">
        <Link to={`/notes/${n.id}`} className="recentrow__title">
          {title}
        </Link>
        <span className="recentrow__meta">
          <span className="muted">{n.kind}</span>
        </span>
      </div>
      <div className="recentrow__actions">
        <NoteQuickActions id={n.id} title={title} tags={n.tags} />
      </div>
    </li>
  )
}
