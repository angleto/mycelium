import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api, authFetch, errMessage, workspaceHeader } from '../api/client'
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

// Web push (#D). The 'webpush' channel is handled out-of-band from the
// typed client (its target lives in push_subscriptions, not the pref), so
// these calls go through authFetch and the channel value is an untyped
// string until the OpenAPI schema is regenerated.
function pushSupported(): boolean {
  return (
    typeof window !== 'undefined' &&
    'serviceWorker' in navigator &&
    'PushManager' in window &&
    typeof Notification !== 'undefined'
  )
}

// VAPID public key (base64url) -> Uint8Array, as PushManager.subscribe
// requires for applicationServerKey.
function urlB64ToUint8Array(base64: string): BufferSource {
  const padding = '='.repeat((4 - (base64.length % 4)) % 4)
  const b64 = (base64 + padding).replace(/-/g, '+').replace(/_/g, '/')
  const raw = atob(b64)
  // Build on an explicit ArrayBuffer so the view is Uint8Array<ArrayBuffer>
  // (assignable to BufferSource; a plain new Uint8Array(n) infers the wider
  // ArrayBufferLike and trips applicationServerKey's type).
  const buf = new ArrayBuffer(raw.length)
  const out = new Uint8Array(buf)
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i)
  return out
}

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
  // Web push: VAPID key (null = unconfigured/unsupported), whether this
  // device is subscribed, and an in-flight guard for the subscribe dance.
  const [vapidKey, setVapidKey] = useState<string | null>(null)
  const [pushOn, setPushOn] = useState(false)
  const [pushBusy, setPushBusy] = useState(false)

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

  // Web push: discover server VAPID config + whether this device is already
  // subscribed. The affordance stays hidden when unsupported/unconfigured.
  useEffect(() => {
    let active = true
    void (async () => {
      if (!pushSupported()) return
      try {
        const res = await authFetch('/notifications/push/vapid-public-key')
        if (!res.ok) return
        const d = (await res.json()) as { configured: boolean; public_key: string }
        if (!active) return
        setVapidKey(d.configured ? d.public_key : null)
        const reg = await navigator.serviceWorker.ready
        const sub = await reg.pushManager.getSubscription()
        if (active) setPushOn(sub !== null && Notification.permission === 'granted')
      } catch {
        /* leave the push affordance hidden on any error */
      }
    })()
    return () => {
      active = false
    }
  }, [activeId])

  async function enableBrowserPush() {
    if (!vapidKey) return
    setErr(null)
    setPushBusy(true)
    try {
      const perm =
        Notification.permission === 'granted'
          ? 'granted'
          : await Notification.requestPermission()
      if (perm !== 'granted') {
        setErr(t('notif.browserPush.denied'))
        return
      }
      const reg = await navigator.serviceWorker.ready
      const sub =
        (await reg.pushManager.getSubscription()) ??
        (await reg.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: urlB64ToUint8Array(vapidKey),
        }))
      const r = await authFetch('/notifications/push/subscribe', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(sub.toJSON()),
      })
      if (!r.ok) {
        setErr(t('error.generic'))
        return
      }
      const uid = currentUserId()
      if (uid) {
        await authFetch('/notifications/prefs', {
          method: 'PUT',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({
            user_id: uid,
            channel: 'webpush',
            enabled: true,
            target: '',
          }),
        })
      }
      setPushOn(true)
      setMsg(t('notif.saved'))
      await reload()
    } finally {
      setPushBusy(false)
    }
  }

  async function disableBrowserPush() {
    setErr(null)
    setPushBusy(true)
    try {
      const reg = await navigator.serviceWorker.ready
      const sub = await reg.pushManager.getSubscription()
      if (sub) {
        await authFetch('/notifications/push/unsubscribe', {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ endpoint: sub.endpoint }),
        })
        await sub.unsubscribe()
      }
      const uid = currentUserId()
      if (uid) {
        await authFetch('/notifications/prefs', {
          method: 'PUT',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({
            user_id: uid,
            channel: 'webpush',
            enabled: false,
            target: '',
          }),
        })
      }
      setPushOn(false)
      setMsg(t('notif.saved'))
      await reload()
    } finally {
      setPushBusy(false)
    }
  }

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

      <h2>{t('notif.browserPush.title')}</h2>
      <div className="row">
        {!pushSupported() ? (
          <span className="muted">{t('notif.browserPush.unsupported')}</span>
        ) : !vapidKey ? (
          <span className="muted">{t('notif.browserPush.unconfigured')}</span>
        ) : (
          <>
            <span className={pushOn ? 'ok' : 'muted'}>
              {pushOn ? t('notif.browserPush.on') : t('notif.browserPush.off')}
            </span>
            <button
              type="button"
              disabled={pushBusy}
              onClick={() => void (pushOn ? disableBrowserPush() : enableBrowserPush())}
            >
              {pushOn ? t('notif.browserPush.disable') : t('notif.browserPush.enable')}
            </button>
          </>
        )}
      </div>
      <p className="hint">{t('notif.browserPush.hint')}</p>

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
