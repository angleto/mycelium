import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import { useSession } from '../auth/useSession'
import { TagChip } from '../components/TagChip'
import type { components } from '../api/schema'

type Task = components['schemas']['TaskOut']
type Tag = components['schemas']['TagOut']
type Running = components['schemas']['TimeEntryOut']

function hms(sec: number): string {
  const s = Math.max(0, Math.floor(sec))
  return `${Math.floor(s / 3600)}:${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`
}

// Tasks as the central productivity surface (Todoist/Toggl-like):
// quick add, colored tag filter, and an inline start/stop timer per
// row so you never leave to track time. The running timer is polled
// (no WS endpoint in v1) so a timer started elsewhere shows here too.
export function TasksRoute() {
  const { t } = useTranslation()
  const session = useSession()
  const activeId = session?.workspaceId
  const [tasks, setTasks] = useState<Task[]>([])
  const [tags, setTags] = useState<Tag[]>([])
  const [filter, setFilter] = useState('')
  const [title, setTitle] = useState('')
  const [running, setRunning] = useState<Running | null>(null)
  const [now, setNow] = useState<number>(() => Date.now())
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const loadTasks = useCallback(async () => {
    setErr(null)
    const { data, error } = await api.GET('/tasks', {
      params: {
        header: workspaceHeader(),
        query: filter ? { tag_id: filter } : {},
      },
    })
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    setTasks(data)
  }, [filter])

  useEffect(() => {
    let active = true
    void (async () => {
      const h = workspaceHeader()
      const [tk, tg] = await Promise.all([
        api.GET('/tasks', {
          params: { header: h, query: filter ? { tag_id: filter } : {} },
        }),
        api.GET('/tags', { params: { header: h } }),
      ])
      if (!active) return
      if (tk.data) setTasks(tk.data)
      else setErr(errMessage(tk.error))
      if (tg.data) setTags(tg.data)
    })()
    return () => {
      active = false
    }
  }, [activeId, filter])

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

  async function onCreate(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setErr(null)
    const { error } = await api.POST('/tasks', {
      params: { header: workspaceHeader() },
      body: { title, priority: 3, executor_kind: 'human', necessity: 'should' },
    })
    setBusy(false)
    if (error) {
      setErr(errMessage(error))
      return
    }
    setTitle('')
    await loadTasks()
  }

  async function toggleTimer(taskId: string) {
    setErr(null)
    const onThis = running?.task_id === taskId
    const call = onThis
      ? api.POST('/time/stop', {
          params: { header: workspaceHeader() },
          body: {},
        })
      : api.POST('/time/start', {
          params: { header: workspaceHeader() },
          body: { task_id: taskId, billable: true },
        })
    const { error } = await call
    if (error) {
      setErr(errMessage(error))
      return
    }
    const { data } = await api.GET('/time/running', {
      params: { header: workspaceHeader() },
    })
    setRunning(data ?? null)
  }

  const activeTag = tags.find((x) => x.id === filter)

  return (
    <section className="card">
      <h1>{t('tasks.title')}</h1>

      <form onSubmit={(e) => void onCreate(e)} className="row">
        <input
          required
          placeholder={t('tasks.quickAdd')}
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          style={{ flex: 1, minWidth: '14rem' }}
        />
        <button type="submit" disabled={busy}>
          {busy ? t('tasks.saving') : t('tasks.create')}
        </button>
      </form>

      <div className="row">
        <label>
          {t('tasks.filterTag')}
          <select value={filter} onChange={(e) => setFilter(e.target.value)}>
            <option value="">{t('tasks.all')}</option>
            {tags.map((tg) => (
              <option key={tg.id} value={tg.id}>
                {tg.kind}: {tg.name}
              </option>
            ))}
          </select>
        </label>
        {activeTag && <TagChip name={activeTag.name} color={activeTag.color} kind={activeTag.kind} />}
      </div>

      {err && <p className="err">{err}</p>}
      {tasks.length === 0 ? (
        <p className="hint">{t('tasks.none')}</p>
      ) : (
        <ul className="list">
          {tasks.map((tk) => {
            const onThis = running?.task_id === tk.id
            const elapsed = onThis
              ? (now - new Date(running.started_at).getTime()) / 1000
              : 0
            return (
              <li key={tk.id}>
                <button
                  type="button"
                  className={onThis ? 'btn--sm' : 'btn--ghost btn--sm'}
                  onClick={() => void toggleTimer(tk.id)}
                  title={onThis ? t('tasks.stop') : t('tasks.start')}
                >
                  {onThis ? `■ ${hms(elapsed)}` : '▶'}
                </button>
                <Link to={`/tasks/${tk.id}`} style={{ fontWeight: 600 }}>
                  {tk.title}
                </Link>
                <span className="muted">
                  {tk.state} · P{tk.priority}
                  {onThis ? ` · ${t('tasks.running')}` : ''}
                </span>
              </li>
            )
          })}
        </ul>
      )}

      <TaxonomyPanel tags={tags} onChanged={() => void loadTasks()} />
    </section>
  )
}

function TaxonomyPanel({ tags, onChanged }: { tags: Tag[]; onChanged: () => void }) {
  const { t } = useTranslation()
  const [clientName, setClientName] = useState('')
  const [ragione, setRagione] = useState('')
  const [projName, setProjName] = useState('')
  const [clientTag, setClientTag] = useState('')
  const [err, setErr] = useState<string | null>(null)
  const clients = tags.filter((x) => x.kind === 'client')

  async function add<T>(p: Promise<{ error?: T }>, reset: () => void) {
    const { error } = await p
    if (error) {
      setErr(errMessage(error))
      return
    }
    setErr(null)
    reset()
    onChanged()
  }

  return (
    <div className="taxonomy">
      <h2>{t('tasks.taxonomy')}</h2>
      {err && <p className="err">{err}</p>}
      <form
        onSubmit={(e) => {
          e.preventDefault()
          void add(
            api.POST('/clients', {
              params: { header: workspaceHeader() },
              body: { name: clientName, ragione_sociale: ragione },
            }),
            () => {
              setClientName('')
              setRagione('')
            },
          )
        }}
      >
        <input
          required
          placeholder={t('tasks.clientName')}
          value={clientName}
          onChange={(e) => setClientName(e.target.value)}
        />
        <input
          required
          placeholder={t('tasks.ragioneSociale')}
          value={ragione}
          onChange={(e) => setRagione(e.target.value)}
        />
        <button type="submit">{t('tasks.addClient')}</button>
      </form>
      <form
        onSubmit={(e) => {
          e.preventDefault()
          void add(
            api.POST('/projects', {
              params: { header: workspaceHeader() },
              body: { name: projName, client_tag_id: clientTag || null, valuta: 'EUR' },
            }),
            () => setProjName(''),
          )
        }}
      >
        <input
          required
          placeholder={t('tasks.projectName')}
          value={projName}
          onChange={(e) => setProjName(e.target.value)}
        />
        <select value={clientTag} onChange={(e) => setClientTag(e.target.value)}>
          <option value="">{t('tasks.all')}</option>
          {clients.map((c) => (
            <option key={c.id} value={c.id}>
              {c.name}
            </option>
          ))}
        </select>
        <button type="submit">{t('tasks.addProject')}</button>
      </form>
    </div>
  )
}
