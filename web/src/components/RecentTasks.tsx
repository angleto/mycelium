import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { PriorityChip } from './PriorityChip'
import { IdentityBadge } from './IdentityBadge'
import { relTime } from '../lib/time'
import type { components } from '../api/schema'

type Task = components['schemas']['TaskOut']

// Persisted UI state (same localStorage pattern as the Tasks view
// toggle): the widget remembers whether the user left it open and how
// many rows they want.
const OPEN_KEY = 'mycelium.tasks.recent.open'
const COUNT_KEY = 'mycelium.tasks.recent.count'
const DEFAULT_COUNT = 4
const MIN_COUNT = 1
const MAX_COUNT = 20

function readOpen(): boolean {
  try {
    const v = localStorage.getItem(OPEN_KEY)
    if (v === '0') return false
    if (v === '1') return true
  } catch {
    /* private mode / quota: fall through to default */
  }
  return true // default open: a shortcut is only useful when visible
}

function readCount(): number {
  try {
    const v = Number(localStorage.getItem(COUNT_KEY))
    if (Number.isFinite(v) && v >= MIN_COUNT && v <= MAX_COUNT) return Math.floor(v)
  } catch {
    /* ignore */
  }
  return DEFAULT_COUNT
}

// updated_at is bumped on every mutation (server TimestampMixin), so it
// doubles as the "created or modified" clock; created_at is the fallback
// for anything that somehow lacks it.
function recencyMs(tk: Task): number {
  return new Date(tk.updated_at ?? tk.created_at).getTime()
}

// Recent-tasks widget pinned at the top of /tasks: the last N tasks by
// modification/creation, newest first, so the user can jump straight to
// what they just touched instead of hunting through the list/board.
// Operates on the focus-filtered task set, so it respects the workspace,
// tag filter and Focus sidebar (client/project) but ignores the transient
// search box and date lens.
export function RecentTasks({ tasks }: { tasks: Task[] }) {
  const { t, i18n } = useTranslation()
  const [open, setOpen] = useState(readOpen)
  const [count, setCount] = useState(readCount)

  useEffect(() => {
    try {
      localStorage.setItem(OPEN_KEY, open ? '1' : '0')
    } catch {
      /* ignore */
    }
  }, [open])
  useEffect(() => {
    try {
      localStorage.setItem(COUNT_KEY, String(count))
    } catch {
      /* ignore */
    }
  }, [count])

  const recent = [...tasks].sort((a, b) => recencyMs(b) - recencyMs(a)).slice(0, count)

  return (
    <section className="recentwidget">
      <div className="recentwidget__head">
        <button
          type="button"
          className="recentwidget__toggle"
          aria-expanded={open}
          onClick={() => setOpen((v) => !v)}
        >
          <span className="recentwidget__caret" aria-hidden="true">
            {open ? '▾' : '▸'}
          </span>
          {t('tasks.recentTitle')}
        </button>
        {open && (
          <label className="recentwidget__count">
            {t('tasks.recentCount')}
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
        (recent.length === 0 ? (
          <p className="hint">{t('tasks.none')}</p>
        ) : (
          <ul className="list recentlist">
            {recent.map((tk) => {
              const score = tk.importance * tk.urgency
              return (
                <li key={tk.id} className="recentrow">
                  <Link to={`/tasks/${tk.id}`} className="recentrow__title">
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
                            tk.created_by_label ?? tk.created_by_handle ?? '',
                        })}
                      />
                    ) : tk.executor_kind === 'llm_agent' ? (
                      <span className="aibadge" title={t('tasks.aiTitle')}>
                        {t('tasks.aiBadge')}
                      </span>
                    ) : null}
                    {tk.title}
                  </Link>
                  <span className="recentrow__meta">
                    <span className="muted">{tk.state}</span>
                    <PriorityChip priority={tk.priority} score={score} />
                    <span className="muted recentrow__when" title={tk.updated_at}>
                      {relTime(tk.updated_at, i18n.language)}
                    </span>
                  </span>
                </li>
              )
            })}
          </ul>
        ))}
    </section>
  )
}
