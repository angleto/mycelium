import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import { useSession } from '../auth/useSession'
import type { components } from '../api/schema'

type Task = components['schemas']['TaskOut']
type Row = components['schemas']['ScheduleOut']

const W = 760
const RH = 30

export function SchedulerRoute() {
  const { t } = useTranslation()
  const session = useSession()
  const activeId = session?.workspaceId
  const [tasks, setTasks] = useState<Task[]>([])
  const [rows, setRows] = useState<Row[]>([])
  const [pinTask, setPinTask] = useState('')
  const [pinDate, setPinDate] = useState('')
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)

  const reload = useCallback(async () => {
    const h = workspaceHeader()
    const [tk, sc] = await Promise.all([
      api.GET('/tasks', { params: { header: h } }),
      api.GET('/schedule', { params: { header: h } }),
    ])
    if (tk.data) setTasks(tk.data)
    if (sc.data) setRows(sc.data)
  }, [])

  useEffect(() => {
    let active = true
    void (async () => {
      const h = workspaceHeader()
      const [tk, sc] = await Promise.all([
        api.GET('/tasks', { params: { header: h } }),
        api.GET('/schedule', { params: { header: h } }),
      ])
      if (!active) return
      if (tk.data) setTasks(tk.data)
      if (sc.data) setRows(sc.data)
    })()
    return () => {
      active = false
    }
  }, [activeId])

  const titleOf = (id: string) => tasks.find((x) => x.id === id)?.title ?? id.slice(0, 8)

  async function onRecompute() {
    setBusy(true)
    setErr(null)
    setMsg(null)
    const { data, error } = await api.POST('/schedule/recompute', {
      params: { header: workspaceHeader() },
      body: {},
    })
    setBusy(false)
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
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

  const dated = rows.filter((r) => r.es && r.ef)
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
      <div className="row">
        <button type="button" onClick={() => void onRecompute()} disabled={busy}>
          {busy ? t('scheduler.recomputing') : t('scheduler.recompute')}
        </button>
        {msg && <span className="ok">{msg}</span>}
        {err && <span className="err">{err}</span>}
      </div>

      {dated.length === 0 ? (
        <p className="hint">{t('scheduler.empty')}</p>
      ) : (
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
                />
                <text x={x + w + 6} y={y + 14} className="dag__lbl">
                  {r.slack_minutes != null ? `${r.slack_minutes}m` : ''}
                </text>
              </g>
            )
          })}
        </svg>
      )}
      <p className="hint">{t('scheduler.critical')}</p>

      <form onSubmit={(e) => void onPin(e)} className="row">
        <label>
          {t('scheduler.pin')}
          <select value={pinTask} onChange={(e) => setPinTask(e.target.value)}>
            <option value="">--</option>
            {tasks.map((x) => (
              <option key={x.id} value={x.id}>
                {x.title}
              </option>
            ))}
          </select>
        </label>
        <input
          type="date"
          value={pinDate}
          onChange={(e) => setPinDate(e.target.value)}
        />
        <button type="submit">{t('scheduler.pin')}</button>
      </form>
    </section>
  )
}
