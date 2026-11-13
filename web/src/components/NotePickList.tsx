import { useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { findNotes } from '../api/notes'
import type { components } from '../shared'

type Note = components['schemas']['NoteListOut']

/** How many candidates one keystroke asks for, and how long typing must
 * pause before it asks. */
const CANDIDATES = 30
const DEBOUNCE_MS = 200

// Searchable picker over the workspace's notes, the note side of
// TaskPickList. It asks the server for the notes matching what is typed
// (title, any part, a tag name), so a note is findable however old it is;
// it used to filter a list its parent had loaded, which held the newest
// 200 notes and nothing older. ``exclude`` drops notes already chosen.
export function NotePickList({
  exclude = [],
  value,
  onPick,
  placeholder,
}: {
  exclude?: string[]
  value: string | null
  onPick: (id: string) => void
  placeholder?: string
}) {
  const { t } = useTranslation()
  const [q, setQ] = useState('')
  const [found, setFound] = useState<Note[]>([])

  useEffect(() => {
    const ac = new AbortController()
    const handle = window.setTimeout(() => {
      void findNotes(q, CANDIDATES, ac.signal)
        .then((rows) => {
          if (!ac.signal.aborted) setFound(rows)
        })
        .catch(() => {
          /* aborted by the next keystroke, or offline: keep the last list */
        })
    }, DEBOUNCE_MS)
    return () => {
      ac.abort()
      window.clearTimeout(handle)
    }
  }, [q])

  const filtered = useMemo(() => {
    const skip = new Set(exclude)
    return found.filter((n) => !n.deleted_at && !n.is_archived && !skip.has(n.id))
  }, [found, exclude])

  return (
    <div className="taskpicklist">
      <div className="row taskpicklist__head">
        <input
          className="taskpicklist__search"
          placeholder={placeholder ?? t('notePicker.search')}
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
      </div>
      {filtered.length === 0 ? (
        <p className="hint taskpicklist__empty">{t('notePicker.none')}</p>
      ) : (
        <ul className="taskpicklist__list">
          {filtered.map((n) => {
            const proj = (n.tags ?? []).find((g) => g.kind === 'project')
            const label =
              n.title?.trim() ||
              (n.preview ?? '').trim() ||
              t('notes.untitled')
            return (
              <li
                key={n.id}
                className={
                  'taskpicklist__item' +
                  (value === n.id ? ' taskpicklist__item--selected' : '')
                }
                onClick={() => onPick(n.id)}
              >
                <span className="taskpicklist__title">{label}</span>
                <span className="muted taskpicklist__meta">
                  {proj && (
                    <span className="chip__glyph">
                      <span
                        aria-hidden="true"
                        style={{ color: proj.color || 'currentColor' }}
                      >
                        ■
                      </span>{' '}
                      {proj.name}
                    </span>
                  )}
                  <span className="taskpicklist__state">{n.kind}</span>
                </span>
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}
