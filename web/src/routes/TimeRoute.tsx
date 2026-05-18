import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import { useSession } from '../auth/useSession'
import type { components } from '../api/schema'

type Task = components['schemas']['TaskOut']
type Entry = components['schemas']['TimeEntryOut']
type Row = components['schemas']['ReportRowOut']
type Group = components['schemas']['ReportGroup']

const GROUPS: Group[] = ['project', 'client', 'generic', 'user', 'task']

function hhmmss(sec: number): string {
  const s = Math.max(0, Math.floor(sec))
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  return `${h}h ${String(m).padStart(2, '0')}m ${String(s % 60).padStart(2, '0')}s`
}

export function TimeRoute() {
  const { t } = useTranslation()
  const session = useSession()
  const activeId = session?.workspaceId
  const [tasks, setTasks] = useState<Task[]>([])
  const [running, setRunning] = useState<Entry | null>(null)
  const [entries, setEntries] = useState<Entry[]>([])
  const [report, setReport] = useState<Row[]>([])
  const [group, setGroup] = useState<Group>('project')
  const [pick, setPick] = useState('')
  const [now, setNow] = useState<number>(() => Date.now())
  const [err, setErr] = useState<string | null>(null)

  const titleOf = (id: string) => tasks.find((x) => x.id === id)?.title ?? id.slice(0, 8)

  const reloadEntries = useCallback(async () => {
    const h = workspaceHeader()
    const [e, r] = await Promise.all([
      api.GET('/time/entries', { params: { header: h } }),
      api.GET('/time/report', { params: { header: h, query: { group_by: group } } }),
    ])
    if (e.data) setEntries(e.data)
    if (r.data) setReport(r.data)
  }, [group])

  // Realtime-ish: poll the running timer (no WS endpoint in v1). State
  // is only set inside the async tick, never sync in the effect body.
  useEffect(() => {
    let active = true
    const tick = async () => {
      const { data } = await api.GET('/time/running', {
        params: { header: workspaceHeader() },
      })
      if (active) setRunning(data ?? null)
    }
    void tick()
    const poll = setInterval(() => void tick(), 5000)
    const clock = setInterval(() => setNow(Date.now()), 1000)
    return () => {
      active = false
      clearInterval(poll)
      clearInterval(clock)
    }
  }, [activeId])

  useEffect(() => {
    let active = true
    void (async () => {
      const h = workspaceHeader()
      const [tk, e, r] = await Promise.all([
        api.GET('/tasks', { params: { header: h } }),
        api.GET('/time/entries', { params: { header: h } }),
        api.GET('/time/report', { params: { header: h, query: { group_by: group } } }),
      ])
      if (!active) return
      if (tk.data) setTasks(tk.data)
      if (e.data) setEntries(e.data)
      if (r.data) setReport(r.data)
    })()
    return () => {
      active = false
    }
  }, [activeId, group])

  async function onStart(e: FormEvent) {
    e.preventDefault()
    if (!pick) return
    setErr(null)
    const { error } = await api.POST('/time/start', {
      params: { header: workspaceHeader() },
      body: { task_id: pick, billable: true },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    const { data } = await api.GET('/time/running', {
      params: { header: workspaceHeader() },
    })
    setRunning(data ?? null)
  }

  async function onStop() {
    setErr(null)
    const { error } = await api.POST('/time/stop', {
      params: { header: workspaceHeader() },
      body: {},
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    setRunning(null)
    await reloadEntries()
  }

  const elapsed = running
    ? (now - new Date(running.started_at).getTime()) / 1000
    : 0

  return (
    <section className="card">
      <h1>{t('time.title')}</h1>
      <p className="hint">{t('time.realtimeNote')}</p>
      {err && <p className="err">{err}</p>}

      {running ? (
        <div className="row">
          <strong>
            {t('time.runningOn')}: {titleOf(running.task_id)}
          </strong>
          <span>
            {t('time.elapsed')}: {hhmmss(elapsed)}
          </span>
          <button type="button" onClick={() => void onStop()}>
            {t('time.stop')}
          </button>
        </div>
      ) : (
        <form onSubmit={(e) => void onStart(e)} className="row">
          <label>
            {t('time.pick')}
            <select value={pick} onChange={(e) => setPick(e.target.value)}>
              <option value="">--</option>
              {tasks.map((x) => (
                <option key={x.id} value={x.id}>
                  {x.title}
                </option>
              ))}
            </select>
          </label>
          <button type="submit">{t('time.start')}</button>
        </form>
      )}

      <h2>{t('time.entries')}</h2>
      {entries.length === 0 ? (
        <p className="hint">{t('time.none')}</p>
      ) : (
        <ul className="list">
          {entries.map((en) => (
            <li key={en.id}>
              {titleOf(en.task_id)}{' '}
              <span className="muted">
                · {en.duration_seconds != null ? hhmmss(en.duration_seconds) : '...'}
                {en.billable ? ` · ${t('time.billable')}` : ''}
              </span>
            </li>
          ))}
        </ul>
      )}

      <h2>
        {t('time.report')}{' '}
        <select value={group} onChange={(e) => setGroup(e.target.value as Group)}>
          {GROUPS.map((g) => (
            <option key={g} value={g}>
              {g}
            </option>
          ))}
        </select>
      </h2>
      <dl className="kv">
        {report.map((r, i) => (
          <div key={i} style={{ display: 'contents' }}>
            <dt>{r.label ?? r.key ?? '-'}</dt>
            <dd>
              {hhmmss(r.seconds)} · {r.amount} {r.currency}
            </dd>
          </div>
        ))}
      </dl>
    </section>
  )
}
