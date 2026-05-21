import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { parseFilter } from '../lib/taskFilter'
import type { components } from '../api/schema'

type Task = components['schemas']['TaskOut']
type State = components['schemas']['StateOut']
type Tag = components['schemas']['TagOut']

// Searchable, scrollable picker over the active task list. Same
// filter DSL as /tasks (parseFilter from lib/taskFilter), so an
// operator who is used to ``@project state:in_progress priority:<=4``
// has the same vocabulary here. Terminal-state tasks are hidden by
// default; a toggle in the header reveals them.
//
// Used by /time in two places (start-timer + edit-entry) and is a
// drop-in for any future surface where a flat <select> of all tasks
// would be unmanageable.
export function TaskPickList({
  tasks,
  tags,
  states,
  value,
  onPick,
  placeholder,
}: {
  tasks: Task[]
  tags: Tag[]
  states: State[]
  value: string | null
  onPick: (id: string) => void
  placeholder?: string
}) {
  const { t } = useTranslation()
  const [q, setQ] = useState('')
  const [showTerminal, setShowTerminal] = useState(false)

  const terminalIds = useMemo(
    () => new Set(states.filter((s) => s.is_terminal).map((s) => s.id)),
    [states],
  )

  const filterCtx = useMemo(
    () => ({
      tagsById: new Map(
        tags.map((g) => [g.id, { name: g.name, kind: g.kind ?? 'generic' }]),
      ),
      statesById: new Map(
        states.map((s) => [
          s.id,
          { name: s.name, is_terminal: s.is_terminal },
        ]),
      ),
      now: new Date(),
    }),
    [tags, states],
  )

  const filtered = useMemo(() => {
    let list = tasks
    if (!showTerminal) list = list.filter((tk) => !terminalIds.has(tk.state_id))
    const needle = q.trim()
    if (needle) {
      const pred = parseFilter(needle, filterCtx)
      list = list.filter(pred)
    }
    // Cap to keep the DOM bounded; the user can refine via the
    // search box if they need to reach a task past the cap.
    return list.slice(0, 200)
  }, [tasks, terminalIds, showTerminal, q, filterCtx])

  return (
    <div className="taskpicklist">
      <div className="row taskpicklist__head">
        <input
          className="taskpicklist__search"
          placeholder={placeholder ?? t('tasks.search')}
          title={t('tasks.searchHint')}
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        <button
          type="button"
          role="switch"
          aria-checked={showTerminal}
          className={
            'toggle-pill toggle-pill--sm' +
            (showTerminal ? ' toggle-pill--on' : '')
          }
          onClick={() => setShowTerminal((v) => !v)}
        >
          {t('time.showTerminal')}:{' '}
          {showTerminal ? t('common.on') : t('common.off')}
        </button>
      </div>
      {filtered.length === 0 ? (
        <p className="hint taskpicklist__empty">{t('tasks.none')}</p>
      ) : (
        <ul className="taskpicklist__list">
          {filtered.map((tk) => {
            const st = states.find((s) => s.id === tk.state_id)
            const proj = (tk.tags ?? []).find((g) => g.kind === 'project')
            return (
              <li
                key={tk.id}
                className={
                  'taskpicklist__item' +
                  (value === tk.id ? ' taskpicklist__item--selected' : '')
                }
                onClick={() => onPick(tk.id)}
              >
                <span className="taskpicklist__title">{tk.title}</span>
                <span className="muted taskpicklist__meta">
                  {proj && (
                    <span
                      className="chip__glyph"
                      style={{ color: proj.color || 'currentColor' }}
                    >
                      ■ {proj.name}
                    </span>
                  )}
                  {st && (
                    <span className="taskpicklist__state">{st.name}</span>
                  )}
                </span>
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}
