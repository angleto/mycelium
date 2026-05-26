import { useTranslation } from 'react-i18next'
import { TagChip } from './TagChip'
import type { components } from '../api/schema'

type Note = components['schemas']['NoteOut']

// Reusable two-row note row: row 1 = title (click to open) + kind /
// status; row 2 = tags then actions. Used wherever notes are listed.
export function NoteListItem({
  note,
  converting,
  derivedTaskTitles,
  onOpen,
  onConvert,
  onPromote,
  onArchive,
  onDelete,
  onErase,
}: {
  note: Note
  converting: boolean
  // Titles of the tasks generated from this note
  // (derived_from + promoted_from). Resolved by the parent route from
  // its already-loaded task list, so the chip never triggers an
  // extra request. Empty when no task has been spawned yet.
  derivedTaskTitles?: string[]
  onOpen: () => void
  onConvert: () => void
  onPromote: () => void
  onArchive: () => void
  onDelete: () => void
  onErase: () => void
}) {
  const { t } = useTranslation()
  const preview = (note.transcript ?? '').split('\n')[0].trim()
  const derivedTitles = derivedTaskTitles ?? []
  // Task 1e07437e: the chip reflects EVERY linked task (subject,
  // artifact, derived_from, promoted_from), not just the two "fruit"
  // kinds the parent route can resolve to titles. Fall back to the
  // derived-titles length when the backend hasn't populated the new
  // field yet (older list payloads, optimistic SPA state).
  const linkedCount =
    typeof note.linked_task_count === 'number'
      ? note.linked_task_count
      : derivedTitles.length
  // Tooltip prefers concrete task titles when available; otherwise
  // shows a generic count so the chip is still self-explanatory for
  // subject-/artifact-only links the parent doesn't know how to title.
  const tooltip =
    derivedTitles.length > 0
      ? derivedTitles.join('\n')
      : t('notes.derivedTasksAria', { count: linkedCount })
  const isPromoted = !!note.promoted_at
  return (
    <li className="noteitem">
      <div className="noteitem__main">
        <button type="button" className="noteitem__title" onClick={onOpen}>
          {note.title || note.kind}
        </button>
        <span className="muted">
          {note.kind} · {note.status}
        </span>
        {linkedCount > 0 && (
          <span
            className="chip chip--derived"
            title={tooltip}
            aria-label={t('notes.derivedTasksAria', { count: linkedCount })}
          >
            {t('notes.derivedTasksCount', { count: linkedCount })}
          </span>
        )}
        {isPromoted && (
          <span className="chip chip--promoted" title={t('notes.promotedHint')}>
            {t('notes.promotedShort')}
          </span>
        )}
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
            title={t('notes.toTaskHint')}
            disabled={converting}
            onClick={onConvert}
          >
            {converting ? t('notes.converting') : t('notes.toTask')}
          </button>
          <button
            type="button"
            className="btn--ghost btn--sm"
            title={t('notes.promoteHint')}
            disabled={converting || isPromoted}
            onClick={onPromote}
          >
            {isPromoted ? t('notes.promotedShort') : t('notes.promote')}
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
