import { useState, type DragEvent } from 'react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import type { components } from '../shared'
import {
  GARDEN_COLUMNS,
  DROP_COLUMNS,
  columnOf,
  moveTargetsOf,
  type GardenColumn,
} from '../lib/gardenBoard'

type Note = components['schemas']['NoteListOut']

// The garden's board (task e584ed61): the notes in four columns, one per
// maturity stage, as an alternative to the tabbed list.
//
// The rule is on the destination, not on the card. Any card but a
// transplanted one may go into growing or mature, from any column, and stays
// there. The maturity sweep (note_links.tick_maturity_transitions) takes a
// growing or mature note to dormant only after 60 days untouched, and the
// move itself is a touch. The value-axis auto-promotion growing -> mature
// (garden_classify.auto_promote_mature, ADR-0032) skips a note a person took
// out of mature, until something moves it again, so a card moved back from
// mature to growing stays growing. A card moved from seed or dormant into
// growing may still be matured by it later, as after the sweep. seed -> growing and
// dormant -> growing are what the sweep would do anyway. The seed and dormant
// columns take no card, because the
// sweep turns a recently touched seed or dormant note into a growing one and
// a maturity change is a touch (note_touch.NOTE_TOUCH_ACTIONS): a card
// dropped there would be moved again by the next sweep. The sweep is off in
// production today; the board is drawn for when it is on, and a column that
// silently undid the person's move would be worse than one that says no.
//
// The list keeps its full maturity selector, so a move into seed or dormant
// is still one click away there.
//
// Drag is HTML5 drag-and-drop, as on the task board, which is jittery on
// touch screens and unreachable from a keyboard. Each movable card therefore
// also carries one button per destination it may go to (at most two).

export function GardenKanban({
  notes,
  busy,
  glyph,
  titleOf,
  previewOf,
  onMove,
  onOpen,
  onArchive,
}: {
  notes: Note[]
  busy: boolean
  glyph: Record<string, string>
  titleOf: (n: Note) => string
  previewOf: (n: Note) => string
  onMove: (n: Note, to: GardenColumn) => Promise<void> | void
  onOpen: (n: Note) => void
  onArchive: (n: Note) => Promise<void> | void
}) {
  const { t } = useTranslation()
  const [draggingId, setDraggingId] = useState<string | null>(null)
  const [hoverColumn, setHoverColumn] = useState<GardenColumn | null>(null)

  const byColumn = new Map<GardenColumn, Note[]>(GARDEN_COLUMNS.map((c) => [c, []]))
  for (const n of notes) byColumn.get(columnOf(n))?.push(n)

  const noteById = (id: string | null) => (id ? notes.find((n) => n.id === id) : undefined)

  function isLegalDrop(id: string | null, to: GardenColumn): boolean {
    const n = noteById(id)
    return n != null && moveTargetsOf(n).includes(to)
  }

  function onDragOver(e: DragEvent<HTMLDivElement>, col: GardenColumn): void {
    // Not calling preventDefault is how a column says no: the browser then
    // shows the no-drop cursor and never fires the drop.
    if (!isLegalDrop(draggingId, col)) return
    e.preventDefault()
    e.dataTransfer.dropEffect = 'move'
    if (hoverColumn !== col) setHoverColumn(col)
  }

  async function onDrop(e: DragEvent<HTMLDivElement>, col: GardenColumn): Promise<void> {
    e.preventDefault()
    const id = e.dataTransfer.getData('text/plain') || draggingId
    setDraggingId(null)
    setHoverColumn(null)
    // Checked again here and not only on dragover: a drop event can arrive
    // without the dragover that would have refused it (another window, a
    // synthetic event), and the rule must hold for it too.
    const n = noteById(id)
    if (!n || busy || !moveTargetsOf(n).includes(col)) return
    await onMove(n, col)
  }

  return (
    <div className="kanban garden__kanban" role="list" aria-label={t('garden.kanban.label')}>
      {GARDEN_COLUMNS.map((col) => {
        const cards = byColumn.get(col) ?? []
        const takesCards = DROP_COLUMNS.has(col)
        const refusing = draggingId != null && !isLegalDrop(draggingId, col)
        const cls =
          'kanban__col' +
          (hoverColumn === col ? ' kanban__col--hover' : '') +
          (refusing && !cards.some((c) => c.id === draggingId) ? ' kanban__col--reject' : '')
        const hintId = `garden-kanban-${col}-hint`
        return (
          <div
            key={col}
            className={cls}
            role="listitem"
            data-column={col}
            aria-label={t(`garden.maturity.${col}`)}
            aria-describedby={takesCards ? undefined : hintId}
            onDragOver={(e) => onDragOver(e, col)}
            onDragLeave={() => hoverColumn === col && setHoverColumn(null)}
            onDrop={(e) => void onDrop(e, col)}
          >
            <header className="kanban__head">
              <span className="kanban__name">
                <span aria-hidden="true">{glyph[col]}</span> {t(`garden.maturity.${col}`)}
              </span>
              <span className="kanban__count">{cards.length}</span>
            </header>
            {!takesCards && (
              <p id={hintId} className="kanban__hint">
                {t('garden.kanban.timeManaged')}
              </p>
            )}
            <ul className="kanban__cards">
              {cards.map((n) => {
                const targets = moveTargetsOf(n)
                const preview = previewOf(n)
                return (
                  <li
                    key={n.id}
                    className={
                      'kanban__card' +
                      (targets.length ? '' : ' kanban__card--fixed') +
                      (draggingId === n.id ? ' kanban__card--dragging' : '')
                    }
                    data-note-id={n.id}
                    draggable={targets.length > 0 && !busy}
                    onDragStart={(e) => {
                      setDraggingId(n.id)
                      e.dataTransfer.effectAllowed = 'move'
                      e.dataTransfer.setData('text/plain', n.id)
                    }}
                    onDragEnd={() => {
                      setDraggingId(null)
                      setHoverColumn(null)
                    }}
                  >
                    <button
                      type="button"
                      className="kanban__title garden__card-title"
                      data-focus-target
                      draggable={false}
                      onClick={() => onOpen(n)}
                      title={t('garden.openPlant')}
                    >
                      {titleOf(n)}
                    </button>
                    {preview && <span className="plant__preview">{preview}</span>}
                    {(n.promoted_at || n.work_closed) && (
                      <div className="kanban__meta">
                        {n.promoted_at && (
                          <span className="plant__chip plant__chip--promoted">
                            {t('garden.promotedChip')}
                          </span>
                        )}
                        {n.work_closed && (
                          <span
                            className="plant__chip plant__chip--closed"
                            title={t('garden.workClosed.hint')}
                          >
                            {t('garden.workClosed.chip')}
                          </span>
                        )}
                      </div>
                    )}
                    <div
                      className="kanban__card-actions"
                      onMouseDown={(e) => e.stopPropagation()}
                      onClick={(e) => e.stopPropagation()}
                    >
                      {targets.map((target) => (
                        <button
                          key={target}
                          type="button"
                          className="btn--ghost btn--sm"
                          disabled={busy}
                          onClick={() => void onMove(n, target)}
                        >
                          <span aria-hidden="true">{glyph[target]}</span>{' '}
                          {t('garden.kanban.moveTo', {
                            stage: t(`garden.maturity.${target}`),
                          })}
                        </button>
                      ))}
                      {n.work_closed && !n.promoted_at && (
                        <button
                          type="button"
                          className="btn--ghost btn--sm"
                          disabled={busy}
                          onClick={() => void onArchive(n)}
                        >
                          {t('garden.archive')}
                        </button>
                      )}
                      <Link
                        to={`/notes/${n.id}`}
                        className="btn--ghost btn--sm"
                        draggable={false}
                        aria-label={t('garden.openNote')}
                        title={t('garden.openNote')}
                      >
                        <span aria-hidden="true">↗</span>
                      </Link>
                    </div>
                  </li>
                )
              })}
              {cards.length === 0 && (
                <li className="kanban__empty">{t('garden.kanban.colEmpty')}</li>
              )}
            </ul>
          </div>
        )
      })}
    </div>
  )
}
