import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import { useSession } from '../auth/useSession'
import { fmtDateTime } from '../lib/tz'
import { DispatchPanel } from '../components/DispatchPanel'
import type { components } from '../api/schema'

type Task = components['schemas']['TaskOut']
type Row = components['schemas']['ScheduleOut']
type Exec = components['schemas']['ExecutorOut']
type Policy = components['schemas']['SchedulePolicy']
type Summary = components['schemas']['RecomputeOut']

const POLICIES: Policy[] = ['balanced', 'fastest', 'cheapest', 'throughput']

const W = 760
const RH = 30

// Slack in minutes → compact human ("0", "3h", "2d 4h").
function humanSlack(m: number | null): string {
  if (m == null) return '—'
  if (m <= 0) return '0'
  const d = Math.floor(m / 1440)
  const h = Math.floor((m % 1440) / 60)
  const mm = m % 60
  return [d && `${d}d`, h && `${h}h`, !d && mm ? `${mm}m` : ''].filter(Boolean).join(' ')
}

export function SchedulerRoute() {
  const { t } = useTranslation()
  const session = useSession()
  const activeId = session?.workspaceId
  const [tasks, setTasks] = useState<Task[]>([])
  const [rows, setRows] = useState<Row[]>([])
  const [executors, setExecutors] = useState<Exec[]>([])
  const [scope, setScope] = useState<'all' | 'mine' | 'ai'>('all')
  const [tagFilter, setTagFilter] = useState('')
  const [pinTask, setPinTask] = useState('')
  const [pinDate, setPinDate] = useState('')
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [policy, setPolicy] = useState<Policy>('balanced')
  const [summary, setSummary] = useState<Summary | null>(null)

  const reload = useCallback(async () => {
    const h = workspaceHeader()
    const [tk, sc, ex] = await Promise.all([
      api.GET('/tasks', { params: { header: h } }),
      api.GET('/schedule', { params: { header: h } }),
      api.GET('/executors', { params: { header: h } }),
    ])
    if (tk.data) setTasks(tk.data)
    if (sc.data) setRows(sc.data)
    if (ex.data) setExecutors(ex.data)
  }, [])

  useEffect(() => {
    let active = true
    void (async () => {
      const h = workspaceHeader()
      const [tk, sc, ex] = await Promise.all([
        api.GET('/tasks', { params: { header: h } }),
        api.GET('/schedule', { params: { header: h } }),
        api.GET('/executors', { params: { header: h } }),
      ])
      if (!active) return
      if (tk.data) setTasks(tk.data)
      if (sc.data) setRows(sc.data)
      if (ex.data) setExecutors(ex.data)
    })()
    return () => {
      active = false
    }
  }, [activeId])

  const titleOf = (id: string) => tasks.find((x) => x.id === id)?.title ?? id.slice(0, 8)
  const execName = (id: string | null | undefined) =>
    id ? (executors.find((e) => e.id === id)?.name ?? '—') : '—'

  async function onRecompute() {
    setBusy(true)
    setErr(null)
    setMsg(null)
    const { data, error } = await api.POST('/schedule/recompute', {
      params: { header: workspaceHeader() },
      body: { policy },
    })
    setBusy(false)
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    setSummary(data)
    setMsg(t('scheduler.computed', { count: data.count }))
    await reload()
  }

  async function onPin(e: FormEvent) {
    e.preventDefault()
    const task = tasks.find((x) => x.id === pinTask)
    if (!task || !pinDate) return
    setErr(null)
    const { error } = await api.PATCH('/tasks/{task_id}/schedule', {
      params: { header: workspaceHeader(), path: { task_id: task.id } },
      body: {
        expected_version: task.version,
        schedule_mode: 'manual',
        constraint_kind: 'SNET',
        constraint_date: `${pinDate}T09:00:00`,
      },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    setMsg(t('scheduler.pinned'))
    await reload()
  }

  const taskById = new Map(tasks.map((x) => [x.id, x]))
  const allTags = [
    ...new Map(
      tasks.flatMap((x) => (x.tags ?? []).map((g) => [g.id, g] as const)),
    ).values(),
  ]
  const dated = rows.filter((r) => {
    if (!r.es || !r.ef) return false
    const tk = taskById.get(r.task_id)
    if (scope === 'mine' && tk?.executor_kind !== 'human') return false
    if (scope === 'ai' && tk?.executor_kind !== 'llm_agent') return false
    if (tagFilter && !(tk?.tags ?? []).some((g) => g.id === tagFilter)) return false
    return true
  })
  const times = dated.flatMap((r) => [
    new Date(r.es as string).getTime(),
    new Date(r.ef as string).getTime(),
  ])
  const min = times.length ? Math.min(...times) : 0
  const max = times.length ? Math.max(...times) : 1
  const span = Math.max(1, max - min)

  return (
    <section className="card">
      <h1>{t('scheduler.title')}</h1>
      <p className="hint">{t('scheduler.intro')}</p>

      <div className="row">
        <label>
          {t('scheduler.policy')}
          <select
            value={policy}
            onChange={(e) => setPolicy(e.target.value as Policy)}
          >
            {POLICIES.map((p) => (
              <option key={p} value={p}>
                {t(`scheduler.policies.${p}`)}
              </option>
            ))}
          </select>
        </label>
        <button type="button" onClick={() => void onRecompute()} disabled={busy}>
          {busy ? t('scheduler.recomputing') : t('scheduler.recompute')}
        </button>
        <span className="muted">{t(`scheduler.policyHint.${policy}`)}</span>
        {msg && <span className="ok">{msg}</span>}
        {err && <span className="err">{err}</span>}
      </div>

      {summary && (
        <p className="hint">
          <strong>{t('scheduler.makespan')}:</strong>{' '}
          {humanSlack(summary.makespan_minutes)}
          {' · '}
          <strong>{t('scheduler.projCost')}:</strong>{' '}
          {summary.projected_credit_cost} {t('scheduler.credits')}
          {' · '}
          {t(`scheduler.policies.${summary.policy}`)}
          {(summary.unassignable_count ?? 0) > 0 && (
            <>
              {' · '}
              <span className="err">
                {t('scheduler.unassignable', {
                  n: summary.unassignable_count,
                })}
              </span>
            </>
          )}
        </p>
      )}

      <h2>{t('dispatch.title')}</h2>
      <DispatchPanel policy={policy} onChanged={() => void reload()} />

      <div className="row">
        <label>
          {t('graph.scopeAll')}
          <select
            value={scope}
            onChange={(e) => setScope(e.target.value as 'all' | 'mine' | 'ai')}
          >
            <option value="all">{t('graph.scopeAll')}</option>
            <option value="mine">{t('graph.scopeMine')}</option>
            <option value="ai">{t('graph.scopeAi')}</option>
          </select>
        </label>
        <label>
          {t('tasks.filterTag')}
          <select value={tagFilter} onChange={(e) => setTagFilter(e.target.value)}>
            <option value="">{t('graph.scopeAll')}</option>
            {allTags.map((g) => (
              <option key={g.id} value={g.id}>
                {g.name}
              </option>
            ))}
          </select>
        </label>
      </div>

      {dated.length === 0 ? (
        <p className="hint">{t('scheduler.empty')}</p>
      ) : (
        <>
          <div className="row sched__legend">
            <span>
              <span className="sched__sw sched__sw--crit" /> {t('scheduler.legendCritical')}
            </span>
            <span>
              <span className="sched__sw" /> {t('scheduler.legendSlack')}
            </span>
            <span className="muted">{t('scheduler.legendChain')}</span>
            <span className="muted">
              {fmtDateTime(new Date(min).toISOString())} →{' '}
              {fmtDateTime(new Date(max).toISOString())}
            </span>
          </div>

          <svg
            className="dag"
            viewBox={`0 0 ${W + 160} ${dated.length * RH + 20}`}
            width="100%"
            role="img"
            aria-label={t('scheduler.title')}
          >
            {dated.map((r, i) => {
              const s = new Date(r.es as string).getTime()
              const e = new Date(r.ef as string).getTime()
              const x = 150 + ((s - min) / span) * W
              const w = Math.max(4, ((e - s) / span) * W)
              const y = 10 + i * RH
              return (
                <g key={r.task_id}>
                  <text x={0} y={y + 16} className="dag__state">
                    {titleOf(r.task_id).slice(0, 22)}
                  </text>
                  <rect
                    x={x}
                    y={y}
                    width={w}
                    height={18}
                    rx={4}
                    className={
                      r.on_logical_critical_path
                        ? 'dag__node dag__node--blocked'
                        : 'dag__node'
                    }
                  >
                    <title>
                      {`${titleOf(r.task_id)}\n${t('scheduler.colEs')}: ${fmtDateTime(r.es as string)}\n${t('scheduler.colEf')}: ${fmtDateTime(r.ef as string)}\n${t('scheduler.slack')}: ${humanSlack(r.slack_minutes)}`}
                    </title>
                  </rect>
                </g>
              )
            })}
          </svg>

          <table className="tbl">
            <thead>
              <tr>
                <th>{t('scheduler.colTask')}</th>
                <th>{t('scheduler.colEs')}</th>
                <th>{t('scheduler.colEf')}</th>
                <th>{t('scheduler.colLs')}</th>
                <th>{t('scheduler.colLf')}</th>
                <th>{t('scheduler.slack')}</th>
                <th>{t('scheduler.colCritical')}</th>
                <th>{t('scheduler.colChain')}</th>
                <th>{t('scheduler.colCost')}</th>
                <th>{t('scheduler.colExecutor')}</th>
              </tr>
            </thead>
            <tbody>
              {dated.map((r) => (
                <tr key={r.task_id}>
                  <td>{titleOf(r.task_id)}</td>
                  <td>{r.es ? fmtDateTime(r.es) : '—'}</td>
                  <td>{r.ef ? fmtDateTime(r.ef) : '—'}</td>
                  <td>{r.ls ? fmtDateTime(r.ls) : '—'}</td>
                  <td>{r.lf ? fmtDateTime(r.lf) : '—'}</td>
                  <td>{humanSlack(r.slack_minutes)}</td>
                  <td>
                    {r.on_logical_critical_path ? (
                      <span className="tag tag--danger">
                        {t('scheduler.criticalYes')}
                      </span>
                    ) : (
                      <span className="muted">—</span>
                    )}
                  </td>
                  <td>
                    {r.on_critical_chain ? (
                      <span className="tag tag--danger">
                        {t('scheduler.chainYes')}
                      </span>
                    ) : (
                      <span className="muted">—</span>
                    )}
                  </td>
                  <td>
                    {Number(r.projected_cost) > 0 ? (
                      `${r.projected_cost} ${t('scheduler.credits')}`
                    ) : (
                      <span className="muted">—</span>
                    )}
                  </td>
                  <td>
                    {r.unassignable ? (
                      <span
                        className="tag tag--danger"
                        title={r.unassignable_reason ?? ''}
                      >
                        {t(
                          `scheduler.unreason.${r.unassignable_reason ?? 'none'}`,
                        )}
                      </span>
                    ) : (
                      execName(r.assigned_executor_id)
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      <h2>{t('scheduler.pin')}</h2>
      <p className="hint">{t('scheduler.pinHint')}</p>
      <form onSubmit={(e) => void onPin(e)} className="row">
        <label>
          {t('scheduler.pinTask')}
          <select value={pinTask} onChange={(e) => setPinTask(e.target.value)}>
            <option value="">--</option>
            {tasks.map((x) => (
              <option key={x.id} value={x.id}>
                {x.title}
              </option>
            ))}
          </select>
        </label>
        <label>
          {t('scheduler.pinDate')}
          <input
            type="date"
            value={pinDate}
            onChange={(e) => setPinDate(e.target.value)}
          />
        </label>
        <button type="submit" disabled={!pinTask || !pinDate}>
          {t('scheduler.pin')}
        </button>
      </form>
    </section>
  )
}
