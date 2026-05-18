import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import type { components } from '../api/schema'

type Tag = components['schemas']['TagOut']
export type TagBriefLike = {
  id: string
  name: string
  color?: string | null
  kind?: string
}

// One reusable tag widget: shows the assigned tags (removable) and a
// searchable, browsable list of the tags that can still be added.
// Used by the task editor, the note editor, anywhere tags are picked
// (no duplicated select code; the catalogue can be large -> search).
export function TagPicker({
  selected,
  all,
  onAdd,
  onRemove,
  disabled,
}: {
  selected: TagBriefLike[]
  all: Tag[]
  onAdd: (id: string) => void
  onRemove: (id: string) => void
  disabled?: boolean
}) {
  const { t } = useTranslation()
  const [q, setQ] = useState('')

  const matches = useMemo(() => {
    const sel = new Set(selected.map((s) => s.id))
    const needle = q.trim().toLowerCase()
    return all
      .filter((g) => g.status !== 'archived' && !sel.has(g.id))
      .filter(
        (g) =>
          !needle || `${g.kind} ${g.name}`.toLowerCase().includes(needle),
      )
      .slice(0, 50)
  }, [all, q, selected])

  return (
    <div className="tagpick">
      <div className="chips">
        {selected.length === 0 && (
          <span className="hint">{t('tagpicker.none')}</span>
        )}
        {selected.map((s) => (
          <button
            key={s.id}
            type="button"
            className="chip chip--rm"
            disabled={disabled}
            title={t('tagpicker.remove')}
            onClick={() => onRemove(s.id)}
          >
            {s.name} ✕
          </button>
        ))}
      </div>
      <input
        className="tagpick__search"
        placeholder={t('tagpicker.search')}
        value={q}
        onChange={(e) => setQ(e.target.value)}
        disabled={disabled}
      />
      <ul className="tagpick__list">
        {matches.length === 0 ? (
          <li className="hint tagpick__empty">{t('tagpicker.noMatch')}</li>
        ) : (
          matches.map((g) => (
            <li key={g.id}>
              <button
                type="button"
                className="tagpick__opt"
                disabled={disabled}
                onClick={() => {
                  onAdd(g.id)
                  setQ('')
                }}
              >
                <span
                  className="chip__dot"
                  style={{ background: g.color || 'var(--accent)' }}
                />
                <span className="muted">{g.kind}:</span> {g.name}
              </button>
            </li>
          ))
        )}
      </ul>
    </div>
  )
}
