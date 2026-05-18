import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import { useSession } from '../auth/useSession'
import { TagChip } from '../components/TagChip'
import { PriorityChip } from '../components/PriorityChip'
import type { components } from '../api/schema'

type Task = components['schemas']['TaskOut']
type Tag = components['schemas']['TagOut']
type Running = components['schemas']['TimeEntryOut']

const SCALE = [1, 2, 3, 4, 5]

function hms(sec: number): string {
  const s = Math.max(0, Math.floor(sec))
  return `${Math.floor(s / 3600)}:${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`
}

// Tasks surface: quick-add with the Eisenhower inputs (importance/
// urgency default 4 -> score 16 -> P1) and client/project pickers with
// inline create; rows are title-left / actions-right with a colored
// priority chip and a clock-play/clock-stop timer.
export function TasksRoute() {
  const { t } = useTranslation()
  const session = useSession()
  const activeId = session?.workspaceId
  const [tasks, setTasks] = useState<Task[]>([])
  const [tags, setTags] = useState<Tag[]>([])
  const [filter, setFilter] = useState('')
  const [title, setTitle] = useState('')
  const [importance, setImportance] = useState(4)
  const [urgency, setUrgency] = useState(4)
  const [clientId, setClientId] = useState('')
  const [projectId, setProjectId] = useState('')
  const [addCli, setAddCli] = useState(false)
  const [addProj, setAddProj] = useState(false)
  const [cName, setCName] = useState('')
  const [cLegal, setCLegal] = useState('')
  const [pName, setPName] = useState('')
  const [running, setRunning] = useState<Running | null>(null)
  const [now, setNow] = useState<number>(() => Date.now())
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const clients = tags.filter((x) => x.kind === 'client')
  const projects = tags.filter((x) => x.kind === 'project')

  const loadTasks = useCallback(async () => {
    setErr(null)
    const { data, error } = await api.GET('/tasks', {
      params: { header: workspaceHeader(), query: filter ? { tag_id: filter } : {} },
    })
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    setTasks(data)
  }, [filter])

  const loadTags = useCallback(async () => {
    const { data } = await api.GET('/tags', { params: { header: workspaceHeader() } })
    if (data) setTags(data)
  }, [])

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
      body: {
        title,
        // priority is required by the schema but the backend derives it
        // from importance x urgency when both are provided.
        priority: 3,
        importance,
        urgency,
        executor_kind: 'human',
        necessity: 'should',
        tag_ids: projectId ? [projectId] : [],
      },
    })
    setBusy(false)
    if (error) {
      setErr(errMessage(error))
      return
    }
    setTitle('')
    await loadTasks()
  }

  async function onAddClient() {
    if (!cName) return
    setErr(null)
    const { data, error } = await api.POST('/clients', {
      params: { header: workspaceHeader() },
      body: { name: cName, ragione_sociale: cLegal || cName },
    })
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    setCName('')
    setCLegal('')
    setAddCli(false)
    await loadTags()
    setClientId(data.id)
  }

  async function onAddProject() {
    if (!pName) return
    setErr(null)
    const { data, error } = await api.POST('/projects', {
      params: { header: workspaceHeader() },
      body: { name: pName, client_tag_id: clientId || null, valuta: 'EUR' },
    })
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    setPName('')
    setAddProj(false)
    await loadTags()
    setProjectId(data.id)
  }

  async function toggleTimer(taskId: string) {
    setErr(null)
    const onThis = running?.task_id === taskId
    const { error } = onThis
      ? await api.POST('/time/stop', { params: { header: workspaceHeader() }, body: {} })
      : await api.POST('/time/start', {
          params: { header: workspaceHeader() },
          body: { task_id: taskId, billable: true },
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

  const activeTag = tags.find((x) => x.id === filter)

  return (
    <section className="card">
      <h1>{t('tasks.title')}</h1>

      <form onSubmit={(e) => void onCreate(e)} className="quickadd">
        <input
          required
          placeholder={t('tasks.quickAdd')}
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          className="quickadd__title"
        />
        <label>
          {t('tasks.importance')}
          <select
            value={importance}
            onChange={(e) => setImportance(Number(e.target.value))}
          >
            {SCALE.map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </label>
        <label>
          {t('tasks.urgency')}
          <select value={urgency} onChange={(e) => setUrgency(Number(e.target.value))}>
            {SCALE.map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </label>
        <label>
          {t('tasks.client')}
          <select value={clientId} onChange={(e) => setClientId(e.target.value)}>
            <option value="">{t('tasks.noClient')}</option>
            {clients.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </select>
        </label>
        <button
          type="button"
          className="btn--ghost btn--sm"
          onClick={() => setAddCli((v) => !v)}
          title={t('tasks.addClient')}
        >
          +
        </button>
        <label>
          {t('tasks.project')}
          <select value={projectId} onChange={(e) => setProjectId(e.target.value)}>
            <option value="">{t('tasks.noProject')}</option>
            {projects.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        </label>
        <button
          type="button"
          className="btn--ghost btn--sm"
          onClick={() => setAddProj((v) => !v)}
          title={t('tasks.addProject')}
        >
          +
        </button>
        <button type="submit" disabled={busy}>
          {busy ? t('tasks.saving') : t('tasks.create')}
        </button>
      </form>

      {addCli && (
        <div className="row">
          <input
            placeholder={t('tasks.newClientName')}
            value={cName}
            onChange={(e) => setCName(e.target.value)}
          />
          <input
            placeholder={t('tasks.legalName')}
            value={cLegal}
            onChange={(e) => setCLegal(e.target.value)}
          />
          <button type="button" className="btn--sm" onClick={() => void onAddClient()}>
            {t('tasks.addInline')}
          </button>
        </div>
      )}
      {addProj && (
        <div className="row">
          <input
            placeholder={t('tasks.newProjectName')}
            value={pName}
            onChange={(e) => setPName(e.target.value)}
          />
          <button type="button" className="btn--sm" onClick={() => void onAddProject()}>
            {t('tasks.addInline')}
          </button>
        </div>
      )}

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
        {activeTag && (
          <TagChip name={activeTag.name} color={activeTag.color} kind={activeTag.kind} />
        )}
      </div>

      {err && <p className="err">{err}</p>}
      {tasks.length === 0 ? (
        <p className="hint">{t('tasks.none')}</p>
      ) : (
        <ul className="list tasklist">
          {tasks.map((tk) => {
            const onThis = running?.task_id === tk.id
            const elapsed = onThis
              ? (now - new Date(running.started_at).getTime()) / 1000
              : 0
            const score =
              tk.importance != null && tk.urgency != null
                ? tk.importance * tk.urgency
                : null
            return (
              <li key={tk.id} className="taskrow">
                <Link to={`/tasks/${tk.id}`} className="taskrow__title">
                  {tk.title}
                </Link>
                <span className="taskrow__meta">
                  <span className="muted">{tk.state}</span>
                  <PriorityChip priority={tk.priority} score={score} />
                  <button
                    type="button"
                    className={onThis ? 'btn--sm' : 'btn--ghost btn--sm'}
                    onClick={() => void toggleTimer(tk.id)}
                    title={onThis ? t('tasks.stop') : t('tasks.start')}
                  >
                    {onThis ? `⏱■ ${hms(elapsed)}` : '⏱▶'}
                  </button>
                </span>
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}
