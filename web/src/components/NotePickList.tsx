import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import type { components } from '../api/schema'

type Note = components['schemas']['NoteOut']

// Searchable scrolling picker over a notes list. Mirror of
// TaskPickList for the note side of LinkedNotesPanel. Filtering is
// pure substring on title (case-insensitive); notes without a title
// fall back to the first non-empty transcript line so a blank-title
// voice note is still locatable.
export function NotePickList({
  notes,
  value,
  onPick,
  placeholder,
}: {
  notes: Note[]
  value: string | null
  onPick: (id: string) => void
  placeholder?: string
}) {
  const { t } = useTranslation()
  const [q, setQ] = useState('')

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase()
    const labelOf = (n: Note) =>
      (n.title?.trim() ||
        (n.transcript ?? '').split('\n').find((s) => s.trim()) ||
        '')
        .toString()
        .toLowerCase()
    let list = notes.filter((n) => !n.deleted_at && !n.is_archived)
    if (needle) list = list.filter((n) => labelOf(n).includes(needle))
    return list.slice(0, 200)
  }, [notes, q])

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
              (n.transcript ?? '').split('\n').find((s) => s.trim()) ||
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
