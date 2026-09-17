import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { fmtDateTime } from '../lib/tz'
import type { Possession, components } from '../shared'

type Worker = components['schemas']['WorkerOut']

// The sessions that are open in this workspace, and what each one is
// holding. Nothing in the app showed this: the possession of ONE task
// was visible on that task, so "how many agents are running right now,
// and on what" had nowhere to be asked.
//
// It is not presence. A worker is a label the server minted, with
// provenance; ``last_seen_at`` is what the server has already observed,
// never what a session claims about itself, and nothing here demands a
// heartbeat for a session to exist. The pathological case this is the
// only place to see: a session open for hours, last seen long ago, and
// still holding a task -- the sweep will reclaim it at the deadline,
// and somebody looking at the board wants to know before then.
export function SessionsPanel({
  workers,
  possessions,
  titles,
}: {
  workers: Worker[]
  possessions: Map<string, Possession>
  titles: Map<string, string>
}) {
  const { t } = useTranslation()
  const held = new Map<string, { taskId: string; possession: Possession }[]>()
  for (const [taskId, possession] of possessions) {
    const w = possession.lease.holder_worker_id
    if (!w) continue
    const bucket = held.get(w) ?? []
    bucket.push({ taskId, possession })
    held.set(w, bucket)
  }
  // Sessions doing something first: a board is read from the top, and a
  // session holding nothing is the one nobody needs to look at.
  const ordered = [...workers].sort((a, b) => {
    const ha = held.get(a.id)?.length ?? 0
    const hb = held.get(b.id)?.length ?? 0
    if (ha !== hb) return hb - ha
    return (b.last_seen_at ?? b.opened_at).localeCompare(a.last_seen_at ?? a.opened_at)
  })
  if (ordered.length === 0) return null
  return (
    <details className="sessions">
      <summary>{t('tasks.sessionsOpen', { count: ordered.length })}</summary>
      <ul className="sessions__list">
        {ordered.map((w) => {
          const mine = held.get(w.id) ?? []
          return (
            <li key={w.id} className="sessions__row">
              <span className="sessions__label">{w.label || w.id.slice(0, 8)}</span>
              <span className="muted">
                {t('tasks.sessionsSeen', {
                  when: fmtDateTime(w.last_seen_at ?? w.opened_at),
                })}
              </span>
              {mine.length === 0 ? (
                <span className="muted">{t('tasks.sessionsIdle')}</span>
              ) : (
                <span className="sessions__holds">
                  {mine.map(({ taskId, possession }) => (
                    <Link key={taskId} to={`/tasks/${taskId}`} className="sessions__task">
                      {titles.get(taskId) ?? taskId.slice(0, 8)}
                      {possession.kind === 'stale' ? ` (${t('tasks.heldLapsed')})` : ''}
                    </Link>
                  ))}
                </span>
              )}
            </li>
          )
        })}
      </ul>
    </details>
  )
}
