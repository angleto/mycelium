import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import { currentUserId } from '../auth/session'
import { useSession } from '../auth/useSession'
import type { components } from '../api/schema'

type Notif = components['schemas']['NotificationOut']
type Task = components['schemas']['TaskOut']
type Channel = components['schemas']['NotificationChannelKind']
type Freq = components['schemas']['RecurrenceFreq']

const CHANNELS: Channel[] = ['telegram', 'email']
const FREQS: Freq[] = ['daily', 'weekly', 'monthly']

export function NotificationsRoute() {
  const { t } = useTranslation()
  const session = useSession()
  const activeId = session?.workspaceId
  const [list, setList] = useState<Notif[]>([])
  const [tasks, setTasks] = useState<Task[]>([])
  const [channel, setChannel] = useState<Channel>('email')
  const [enabled, setEnabled] = useState(true)
  const [target, setTarget] = useState('')
  const [recTask, setRecTask] = useState('')
  const [freq, setFreq] = useState<Freq>('weekly')
  const [nextRun, setNextRun] = useState('')
  const [interval, setIntervalN] = useState(1)
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)

  const reload = useCallback(async () => {
    const h = workspaceHeader()
    const [n, tk] = await Promise.all([
      api.GET('/notifications', { params: { header: h } }),
      api.GET('/tasks', { params: { header: h } }),
    ])
    if (n.data) setList(n.data)
    if (tk.data) setTasks(tk.data)
  }, [])

  useEffect(() => {
    let active = true
    void (async () => {
      const h = workspaceHeader()
      const [n, tk] = await Promise.all([
        api.GET('/notifications', { params: { header: h } }),
        api.GET('/tasks', { params: { header: h } }),
      ])
      if (!active) return
      if (n.data) setList(n.data)
      if (tk.data) setTasks(tk.data)
    })()
    return () => {
      active = false
    }
  }, [activeId])

  async function onSavePref(e: FormEvent) {
    e.preventDefault()
    const uid = currentUserId()
    if (!uid) return
    setErr(null)
    const { error } = await api.PUT('/notifications/prefs', {
      params: { header: workspaceHeader() },
      body: { user_id: uid, channel, enabled, target },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    setMsg(t('notif.saved'))
  }

  async function onDispatch() {
    setErr(null)
    const { data, error } = await api.POST('/notifications/dispatch', {
      params: { header: workspaceHeader() },
    })
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    setMsg(t('notif.dispatched', { sent: data.sent, failed: data.failed }))
    await reload()
  }

  async function onAddRec(e: FormEvent) {
    e.preventDefault()
    if (!recTask || !nextRun) return
    setErr(null)
    const { error } = await api.POST('/notifications/recurrences', {
      params: { header: workspaceHeader() },
      body: {
        task_id: recTask,
        freq,
        next_run: `${nextRun}:00`,
        interval,
      },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    setMsg(t('notif.saved'))
  }

  async function counted(
    p: Promise<{ data?: { count: number }; error?: unknown }>,
  ) {
    setErr(null)
    const { data, error } = await p
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    setMsg(t('notif.count', { n: data.count }))
    await reload()
  }

  return (
    <section className="card">
      <h1>{t('notif.title')}</h1>
      {err && <p className="err">{err}</p>}
      {msg && <p className="ok">{msg}</p>}

      <form onSubmit={(e) => void onSavePref(e)} className="row">
        <h2>{t('notif.prefs')}</h2>
        <select
          value={channel}
          onChange={(e) => setChannel(e.target.value as Channel)}
        >
          {CHANNELS.map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </select>
        <label>
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => setEnabled(e.target.checked)}
          />{' '}
          {t('notif.enabled')}
        </label>
        <input
          placeholder={t('notif.target')}
          value={target}
          onChange={(e) => setTarget(e.target.value)}
        />
        <button type="submit">{t('notif.savePref')}</button>
      </form>

      <div className="row">
        <button type="button" onClick={() => void onDispatch()}>
          {t('notif.dispatch')}
        </button>
        <button
          type="button"
          onClick={() =>
            void counted(
              api.POST('/notifications/recurrences/spawn-due', {
                params: { header: workspaceHeader() },
              }),
            )
          }
        >
          {t('notif.spawn')}
        </button>
        <button
          type="button"
          onClick={() =>
            void counted(
              api.POST('/notifications/reminders/scan', {
                params: { header: workspaceHeader() },
              }),
            )
          }
        >
          {t('notif.scan')}
        </button>
      </div>

      <form onSubmit={(e) => void onAddRec(e)} className="row">
        <h2>{t('notif.recurrence')}</h2>
        <select value={recTask} onChange={(e) => setRecTask(e.target.value)}>
          <option value="">{t('notif.task')}</option>
          {tasks.map((x) => (
            <option key={x.id} value={x.id}>
              {x.title}
            </option>
          ))}
        </select>
        <select value={freq} onChange={(e) => setFreq(e.target.value as Freq)}>
          {FREQS.map((f) => (
            <option key={f} value={f}>
              {f}
            </option>
          ))}
        </select>
        <input
          type="datetime-local"
          value={nextRun}
          onChange={(e) => setNextRun(e.target.value)}
        />
        <input
          type="number"
          min={1}
          value={interval}
          onChange={(e) => setIntervalN(Number(e.target.value))}
        />
        <button type="submit">{t('notif.addRec')}</button>
      </form>

      <h2>{t('notif.list')}</h2>
      {list.length === 0 ? (
        <p className="hint">{t('notif.none')}</p>
      ) : (
        <ul className="list">
          {list.map((n) => (
            <li key={n.id}>
              {n.kind} <span className="muted">· {n.channel}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
