import { useState, type DragEvent } from 'react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { PriorityChip } from './PriorityChip'
import { TagChip } from './TagChip'
import { TaskTimer } from './TaskTimer'
import { IdentityBadge } from './IdentityBadge'
import type { components } from '../api/schema'
import { formatDueDate } from '../lib/time'

type Task = components['schemas']['TaskOut']
type State = components['schemas']['StateOut']

// Kanban board for the Tasks view. Columns map to workflow states in
// their canonical order (workflow_states.position, mirrored by the order
// the API returns). Within each column tasks are sorted by priority
// ASC — smaller priority number means higher rank (1 = critical), so
// the most important card sits on top.
//
// Drag-and-drop changes a task's state. The workflow transition graph
// (``allowed``) is honoured: a drop into a column that is not reachable
// from the task's current state is rejected (no API call), and the user
// sees the rejection by the card snapping back. Touch DnD is best-effort
// (HTML5 dnd works on iOS/Android browsers but is jittery — mobile users
// default to the list view, so this surface is desktop-first).
export function TaskKanban({
  tasks,
  states,
  allowed,
  onChangeState,
}: {
  tasks: Task[]
  states: State[]
  allowed: Map<string, Set<string>>
  onChangeState: (task: Task, toStateId: string) => Promise<void> | void
}) {
  const { t } = useTranslation()
  const [draggingId, setDraggingId] = useState<string | null>(null)
  const [hoverColumn, setHoverColumn] = useState<string | null>(null)

  const byState = new Map<string, Task[]>()
  for (const s of states) byState.set(s.id, [])
  for (const tk of tasks) {
    const bucket = byState.get(tk.state_id)
    if (bucket) bucket.push(tk)
  }
  // Priority ASC = smallest first (1 = highest). null priority sinks to
  // the bottom so an un-prioritised card never crowds the top.
  for (const bucket of byState.values()) {
    bucket.sort((a, b) => {
      const pa = a.priority ?? Number.POSITIVE_INFINITY
      const pb = b.priority ?? Number.POSITIVE_INFINITY
      if (pa !== pb) return pa - pb
      // Tie-break: due date ascending (sooner first), then title.
      if (a.due_date && b.due_date && a.due_date !== b.due_date) {
        return a.due_date < b.due_date ? -1 : 1
      }
      if (!!a.due_date !== !!b.due_date) return a.due_date ? -1 : 1
      return a.title.localeCompare(b.title)
    })
  }

  function onCardDragStart(e: DragEvent<HTMLLIElement>, id: string): void {
    setDraggingId(id)
    e.dataTransfer.effectAllowed = 'move'
    e.dataTransfer.setData('text/plain', id)
  }

  function onCardDragEnd(): void {
    setDraggingId(null)
    setHoverColumn(null)
  }

  function isLegalDrop(taskId: string, toStateId: string): boolean {
    const tk = tasks.find((x) => x.id === taskId)
    if (!tk) return false
    if (tk.state_id === toStateId) return false
    return allowed.get(tk.state_id)?.has(toStateId) ?? false
  }

  function onColDragOver(e: DragEvent<HTMLDivElement>, stateId: string): void {
    if (!draggingId) return
    if (!isLegalDrop(draggingId, stateId)) return
    e.preventDefault()
    e.dataTransfer.dropEffect = 'move'
    if (hoverColumn !== stateId) setHoverColumn(stateId)
  }

  function onColDragLeave(stateId: string): void {
    if (hoverColumn === stateId) setHoverColumn(null)
  }

  async function onColDrop(
    e: DragEvent<HTMLDivElement>,
    stateId: string,
  ): Promise<void> {
    e.preventDefault()
    const id = e.dataTransfer.getData('text/plain') || draggingId
    setDraggingId(null)
    setHoverColumn(null)
    if (!id) return
    const tk = tasks.find((x) => x.id === id)
    if (!tk) return
    if (tk.state_id === stateId) return
    if (!allowed.get(tk.state_id)?.has(stateId)) return
    await onChangeState(tk, stateId)
  }

  if (states.length === 0) {
    return <p className="hint">{t('tasks.kanbanEmpty')}</p>
  }

  return (
    <div className="kanban" role="list">
      {states.map((s) => {
        const cards = byState.get(s.id) ?? []
        const dropOk = draggingId != null && isLegalDrop(draggingId, s.id)
        const cls =
          'kanban__col' +
          (hoverColumn === s.id ? ' kanban__col--hover' : '') +
          (draggingId && !dropOk && draggingId !== '' && !cards.some((c) => c.id === draggingId)
            ? ' kanban__col--reject'
            : '') +
          (s.is_terminal ? ' kanban__col--terminal' : '') +
          (s.is_initial ? ' kanban__col--initial' : '')
        return (
          <div
            key={s.id}
            className={cls}
            role="listitem"
            onDragOver={(e) => onColDragOver(e, s.id)}
            onDragLeave={() => onColDragLeave(s.id)}
            onDrop={(e) => void onColDrop(e, s.id)}
          >
            <header className="kanban__head">
              <span className="kanban__name">{s.name}</span>
              <span className="kanban__count">{cards.length}</span>
            </header>
            <ul className="kanban__cards">
              {cards.map((tk) => {
                const score = tk.importance * tk.urgency
                return (
                  <li
                    key={tk.id}
                    className={
                      'kanban__card' +
                      (draggingId === tk.id ? ' kanban__card--dragging' : '')
                    }
                    draggable
                    onDragStart={(e) => onCardDragStart(e, tk.id)}
                    onDragEnd={onCardDragEnd}
                  >
                    {/* draggable={false} on the inner Link so the
                        browser starts the LI's HTML5 drag instead of
                        the anchor's native drag (which would otherwise
                        carry just the href, never firing the column's
                        drop). Click still navigates to /tasks/{id}. */}
                    <div className="kanban__card-head">
                      <Link
                        to={`/tasks/${tk.id}`}
                        className="kanban__title"
                        draggable={false}
                      >
                        {tk.assignee_kind ? (
                          <IdentityBadge
                            kind={tk.assignee_kind}
                            handle={tk.assignee_handle ?? null}
                          />
                        ) : tk.created_by_kind === 'ai_assistant' ||
                          tk.created_by_kind === 'mcp_token' ? (
                          <IdentityBadge
                            kind={tk.created_by_kind}
                            handle={tk.created_by_handle ?? null}
                            label={tk.created_by_label ?? null}
                            title={t('tasks.aiCreatedTitle', {
                              handle:
                                tk.created_by_label ??
                                tk.created_by_handle ??
                                '',
                            })}
                          />
                        ) : tk.executor_kind === 'llm_agent' ? (
                          <span
                            className="aibadge"
                            title={t('tasks.aiTitle')}
                          >
                            {t('tasks.aiBadge')}
                          </span>
                        ) : null}
                        {tk.title}
                      </Link>
                      {/* Shared timer widget, pinned top-right. The
                          wrapper stops mousedown/click from bubbling into
                          the LI's dragstart heuristic. */}
                      <div
                        className="kanban__card-actions"
                        onMouseDown={(e) => e.stopPropagation()}
                        onClick={(e) => e.stopPropagation()}
                      >
                        <TaskTimer taskId={tk.id} />
                      </div>
                    </div>
                    <div className="kanban__meta">
                      <PriorityChip priority={tk.priority} score={score} />
                      {tk.start_at && tk.duration_minutes ? (
                        <span
                          className="muted"
                          title={t('tasks.eventTitle', {
                            when: new Date(tk.start_at).toLocaleString(),
                            minutes: tk.duration_minutes,
                          })}
                        >
                          🕒{' '}
                          {new Date(tk.start_at).toLocaleString([], {
                            month: 'short',
                            day: '2-digit',
                            hour: '2-digit',
                            minute: '2-digit',
                          })}
                          {' · '}
                          {tk.duration_minutes}m
                        </span>
                      ) : tk.due_date ? (
                        <span className="muted" title={t('tasks.due')}>
                          📅 {formatDueDate(tk.due_date)}
                        </span>
                      ) : null}
                    </div>
                    {tk.tags && tk.tags.length > 0 && (
                      <div className="kanban__tags">
                        {tk.tags.map((g) => (
                          <TagChip
                            key={g.id}
                            name={g.name}
                            color={g.color}
                            kind={g.kind}
                          />
                        ))}
                      </div>
                    )}
                  </li>
                )
              })}
              {cards.length === 0 && (
                <li className="kanban__empty">{t('tasks.kanbanColEmpty')}</li>
              )}
            </ul>
          </div>
        )
      })}
    </div>
  )
}
