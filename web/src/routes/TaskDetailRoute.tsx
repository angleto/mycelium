import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import { RichEditor } from '../components/RichEditor'
import { PriorityChip } from '../components/PriorityChip'

const SCALE = [1, 2, 3, 4, 5]
function derivePriority(imp: number, urg: number): number {
  const s = imp * urg
  if (s >= 16) return 1
  if (s >= 9) return 2
  if (s >= 4) return 3
  return 4
}
import type { components } from '../api/schema'

type Task = components['schemas']['TaskOut']
type State = components['schemas']['StateOut']
type Tag = components['schemas']['TagOut']
type Dep = components['schemas']['DependencyOut']

// Task detail with optimistic concurrency: edits send expected_version;
// a stale write yields 409 and we reload the canonical task.
export function TaskDetailRoute() {
  const { t } = useTranslation()
  const { id = '' } = useParams()
  const [task, setTask] = useState<Task | null>(null)
  const [states, setStates] = useState<State[]>([])
  const [tags, setTags] = useState<Tag[]>([])
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [importance, setImportance] = useState(4)
  const [urgency, setUrgency] = useState(4)
  const [stateId, setStateId] = useState('')
  const [tagId, setTagId] = useState('')
  const [allTasks, setAllTasks] = useState<Task[]>([])
  const [deps, setDeps] = useState<Dep[]>([])
  const [depOther, setDepOther] = useState('')
  const [depRel, setDepRel] = useState<'depends' | 'blocks'>('depends')
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)

  const apply = useCallback((tk: Task) => {
    setTask(tk)
    setTitle(tk.title)
    setDescription(tk.description ?? '')
    setImportance(tk.importance ?? 4)
    setUrgency(tk.urgency ?? 4)
    setStateId(tk.state_id)
  }, [])

  const reload = useCallback(async () => {
    setErr(null)
    const { data, error } = await api.GET('/tasks/{task_id}', {
      params: { header: workspaceHeader(), path: { task_id: id } },
    })
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    apply(data)
  }, [id, apply])

  const reloadDeps = useCallback(async () => {
    const { data } = await api.GET('/dependencies', {
      params: { header: workspaceHeader() },
    })
    if (data) setDeps(data)
  }, [])

  useEffect(() => {
    let active = true
    void (async () => {
      const h = workspaceHeader()
      const [tk, st, tg, all, dp] = await Promise.all([
        api.GET('/tasks/{task_id}', { params: { header: h, path: { task_id: id } } }),
        api.GET('/tasks/{task_id}/states', {
          params: { header: h, path: { task_id: id } },
        }),
        api.GET('/tags', { params: { header: h } }),
        api.GET('/tasks', { params: { header: h } }),
        api.GET('/dependencies', { params: { header: h } }),
      ])
      if (!active) return
      if (tk.data) apply(tk.data)
      else setErr(errMessage(tk.error))
      if (st.data) setStates(st.data)
      if (tg.data) setTags(tg.data)
      if (all.data) setAllTasks(all.data)
      if (dp.data) setDeps(dp.data)
    })()
    return () => {
      active = false
    }
  }, [id, apply])

  async function onSave(e: FormEvent) {
    e.preventDefault()
    if (!task) return
    setBusy(true)
    setMsg(null)
    setErr(null)
    const { error, response } = await api.PATCH('/tasks/{task_id}', {
      params: { header: workspaceHeader(), path: { task_id: id } },
      body: {
        expected_version: task.version,
        title,
        description: description || null,
        importance,
        urgency,
      },
    })
    setBusy(false)
    if (response.status === 409) {
      setErr(t('tasks.conflict'))
      await reload()
      return
    }
    if (error) {
      setErr(errMessage(error))
      return
    }
    await reload()
    setMsg(t('tasks.saved'))
  }

  async function onChangeState() {
    if (!task) return
    setErr(null)
    const { error, response } = await api.POST('/tasks/{task_id}/state', {
      params: { header: workspaceHeader(), path: { task_id: id } },
      body: { expected_version: task.version, state_id: stateId },
    })
    if (response.status === 409) {
      setErr(t('tasks.conflict'))
      await reload()
      return
    }
    if (error) {
      setErr(errMessage(error))
      return
    }
    await reload()
  }

  async function onAddTag() {
    if (!tagId) return
    setErr(null)
    const { error } = await api.POST('/tasks/{task_id}/tags', {
      params: { header: workspaceHeader(), path: { task_id: id } },
      body: { tag_id: tagId },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    setMsg(t('tasks.saved'))
  }

  async function onAddDep() {
    if (!depOther) return
    setErr(null)
    // depends-on: the other task must finish first (it is the
    // predecessor). blocks: this task is the predecessor. FS edges feed
    // the deterministic scheduler; cycles are rejected server-side.
    const body =
      depRel === 'depends'
        ? { predecessor_id: depOther, successor_id: id, type: 'FS' as const }
        : { predecessor_id: id, successor_id: depOther, type: 'FS' as const }
    const { error } = await api.POST('/dependencies', {
      params: { header: workspaceHeader() },
      body: { ...body, lag_working_minutes: 0 },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    setDepOther('')
    await reloadDeps()
  }

  async function onRemoveDep(depId: string) {
    setErr(null)
    const { error } = await api.DELETE('/dependencies/{dependency_id}', {
      params: { header: workspaceHeader(), path: { dependency_id: depId } },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    await reloadDeps()
  }

  const titleOf = (tid: string) =>
    allTasks.find((x) => x.id === tid)?.title ?? tid.slice(0, 8)
  const dependsOn = deps.filter((d) => d.successor_id === id)
  const blocks = deps.filter((d) => d.predecessor_id === id)

  if (err && !task) return <p className="err">{err}</p>
  if (!task) return <p>{t('tasks.loading')}</p>

  return (
    <section className="card">
      <p className="hint">
        <Link to="/tasks">{t('tasks.back')}</Link>
      </p>
      <form onSubmit={(e) => void onSave(e)}>
        <label>
          {t('tasks.newTitle')}
          <input
            required
            value={title}
            onChange={(e) => setTitle(e.target.value)}
          />
        </label>
        <label>
          {t('tasks.description')}
          <RichEditor value={description} onChange={setDescription} />
        </label>
        <div className="row">
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
            <select
              value={urgency}
              onChange={(e) => setUrgency(Number(e.target.value))}
            >
              {SCALE.map((n) => (
                <option key={n} value={n}>
                  {n}
                </option>
              ))}
            </select>
          </label>
          <PriorityChip
            priority={derivePriority(importance, urgency)}
            score={importance * urgency}
          />
        </div>
        {msg && <p className="ok">{msg}</p>}
        {err && <p className="err">{err}</p>}
        <button type="submit" disabled={busy}>
          {busy ? t('tasks.saving') : t('tasks.save')}
        </button>
      </form>

      <div className="row">
        <label>
          {t('tasks.state')}
          <select value={stateId} onChange={(e) => setStateId(e.target.value)}>
            {states.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </select>
        </label>
        <button type="button" onClick={() => void onChangeState()}>
          {t('tasks.save')}
        </button>
      </div>

      <div className="row">
        <label>
          {t('tasks.addTag')}
          <select value={tagId} onChange={(e) => setTagId(e.target.value)}>
            <option value="">{t('tasks.all')}</option>
            {tags.map((tg) => (
              <option key={tg.id} value={tg.id}>
                {tg.kind}: {tg.name}
              </option>
            ))}
          </select>
        </label>
        <button type="button" onClick={() => void onAddTag()}>
          {t('tasks.addTag')}
        </button>
      </div>

      <h2>{t('tasks.deps')}</h2>
      {dependsOn.length === 0 && blocks.length === 0 ? (
        <p className="hint">{t('tasks.depNone')}</p>
      ) : (
        <ul className="list">
          {dependsOn.map((d) => (
            <li key={d.id}>
              <strong>{t('tasks.dependsOn')}:</strong> {titleOf(d.predecessor_id)}
              <button
                type="button"
                className="btn--ghost btn--sm"
                onClick={() => void onRemoveDep(d.id)}
              >
                {t('tasks.remove')}
              </button>
            </li>
          ))}
          {blocks.map((d) => (
            <li key={d.id}>
              <strong>{t('tasks.blocksL')}:</strong> {titleOf(d.successor_id)}
              <button
                type="button"
                className="btn--ghost btn--sm"
                onClick={() => void onRemoveDep(d.id)}
              >
                {t('tasks.remove')}
              </button>
            </li>
          ))}
        </ul>
      )}
      <div className="row">
        <label>
          {t('tasks.relation')}
          <select
            value={depRel}
            onChange={(e) => setDepRel(e.target.value as 'depends' | 'blocks')}
          >
            <option value="depends">{t('tasks.dependsOn')}</option>
            <option value="blocks">{t('tasks.blocksL')}</option>
          </select>
        </label>
        <label>
          {t('tasks.otherTask')}
          <select value={depOther} onChange={(e) => setDepOther(e.target.value)}>
            <option value="">--</option>
            {allTasks
              .filter((x) => x.id !== id)
              .map((x) => (
                <option key={x.id} value={x.id}>
                  {x.title}
                </option>
              ))}
          </select>
        </label>
        <button type="button" onClick={() => void onAddDep()}>
          {t('tasks.addDep')}
        </button>
      </div>
      <p className="hint">{t('tasks.relatedTo')}</p>
    </section>
  )
}
