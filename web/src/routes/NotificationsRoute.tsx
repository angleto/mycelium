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
type Pref = components['schemas']['NotificationPrefOut']

const CHANNELS: Channel[] = ['telegram', 'email']
const FREQS: Freq[] = ['daily', 'weekly', 'monthly', 'yearly']

export function NotificationsRoute() {
  const { t } = useTranslation()
  const session = useSession()
  const activeId = session?.workspaceId
  const [list, setList] = useState<Notif[]>([])
  const [prefs, setPrefs] = useState<Pref[]>([])
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
    const [n, tk, pf] = await Promise.all([
      api.GET('/notifications', { params: { header: h } }),
      api.GET('/tasks', { params: { header: h } }),
      api.GET('/notifications/prefs', { params: { header: h } }),
    ])
    if (n.data) setList(n.data)
    if (tk.data) setTasks(tk.data)
    if (pf.data) setPrefs(pf.data)
  }, [])

  useEffect(() => {
    let active = true
    void (async () => {
      const h = workspaceHeader()
      const [n, tk, pf] = await Promise.all([
        api.GET('/notifications', { params: { header: h } }),
        api.GET('/tasks', { params: { header: h } }),
        api.GET('/notifications/prefs', { params: { header: h } }),
      ])
      if (!active) return
      if (n.data) setList(n.data)
      if (tk.data) setTasks(tk.data)
      if (pf.data) setPrefs(pf.data)
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
    // A channel is usable only once configured: no target -> not enabled.
    const { error } = await api.PUT('/notifications/prefs', {
      params: { header: workspaceHeader() },
      body: {
        user_id: uid,
        channel,
        enabled: target.trim() !== '' && enabled,
        target,
      },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    setMsg(t('notif.saved'))
  }

  async function onDelete(id: string) {
    setErr(null)
    const { error } = await api.DELETE('/notifications/{notification_id}', {
      params: { header: workspaceHeader(), path: { notification_id: id } },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    setList((xs) => xs.filter((x) => x.id !== id))
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

      <h2>{t('notif.prefs')}</h2>
      <p className="hint">{t('notif.hintPrefs')}</p>
      <ul className="list">
        {CHANNELS.map((c) => {
          const p = prefs.find((x) => x.channel === c)
          const on = p && p.enabled && p.target
          return (
            <li key={c}>
              <strong>{c}</strong>{' '}
              <span className={on ? 'ok' : 'muted'}>
                {on
                  ? `${t('notif.configured')} (${p?.target})`
                  : t('notif.notConfigured')}
              </span>
            </li>
          )
        })}
      </ul>
      <form onSubmit={(e) => void onSavePref(e)} className="row">
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
        <input
          placeholder={t('notif.target')}
          value={target}
          onChange={(e) => setTarget(e.target.value)}
        />
        <label title={target.trim() === '' ? t('notif.needTarget') : undefined}>
          <input
            type="checkbox"
            checked={enabled}
            disabled={target.trim() === ''}
            onChange={(e) => setEnabled(e.target.checked)}
          />{' '}
          {t('notif.enabled')}
        </label>
        <button type="submit">{t('notif.savePref')}</button>
      </form>

      <p className="hint">{t('notif.hintActions')}</p>
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
              {t(`notif.freqOpt.${f}`)}
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
            <li key={n.id} className="row">
              <span>
                <strong>{n.title || n.kind}</strong>{' '}
                <span className="muted">· {n.channel}</span>{' '}
                <span
                  className={
                    n.status === 'sent'
                      ? 'ok'
                      : n.status === 'failed'
                        ? 'err'
                        : 'muted'
                  }
                >
                  {t(`notif.status.${n.status}`)}
                </span>
                {n.created_at && (
                  <span className="muted">
                    {' '}
                    · {new Date(n.created_at).toLocaleString()}
                  </span>
                )}
              </span>
              <button
                type="button"
                className="link"
                onClick={() => void onDelete(n.id)}
                title={t('notif.delete')}
              >
                {t('notif.delete')}
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
