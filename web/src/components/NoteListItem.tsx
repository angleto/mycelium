import { useTranslation } from 'react-i18next'
import { TagChip } from './TagChip'
import type { components } from '../api/schema'

type Note = components['schemas']['NoteOut']

// Reusable two-row note row: row 1 = title (click to open) + kind /
// status; row 2 = tags then actions. Used wherever notes are listed.
export function NoteListItem({
  note,
  converting,
  converted,
  onOpen,
  onConvert,
  onArchive,
  onDelete,
  onErase,
}: {
  note: Note
  converting: boolean
  converted: boolean
  onOpen: () => void
  onConvert: () => void
  onArchive: () => void
  onDelete: () => void
  onErase: () => void
}) {
  const { t } = useTranslation()
  const preview = (note.transcript ?? '').split('\n')[0].trim()
  return (
    <li className="noteitem">
      <div className="noteitem__main">
        <button type="button" className="noteitem__title" onClick={onOpen}>
          {note.title || note.kind}
        </button>
        <span className="muted">
          {note.kind} · {note.status}
        </span>
        {preview && <span className="noteitem__preview">{preview}</span>}
      </div>
      <div className="noteitem__meta">
        <span className="noteitem__tags">
          {(note.tags ?? []).map((g) => (
            <TagChip key={g.id} name={g.name} color={g.color} kind={g.kind} />
          ))}
        </span>
        <span className="noteitem__actions">
          <button
            type="button"
            className="btn--sm"
            disabled={converting || converted}
            onClick={onConvert}
          >
            {converting
              ? t('notes.converting')
              : converted
                ? t('notes.convertedShort')
                : t('notes.toTask')}
          </button>
          <button
            type="button"
            className="btn--ghost btn--sm"
            onClick={onArchive}
          >
            {t('notes.archive')}
          </button>
          <button
            type="button"
            className="btn--ghost btn--sm"
            onClick={onDelete}
          >
            {t('notes.deleteBtn')}
          </button>
          <button
            type="button"
            className="btn--danger btn--sm"
            title={t('notes.eraseHint')}
            onClick={onErase}
          >
            {t('notes.erase')}
          </button>
        </span>
      </div>
    </li>
  )
}
