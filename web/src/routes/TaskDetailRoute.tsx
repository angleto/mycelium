import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import type { components } from '../api/schema'

type Task = components['schemas']['TaskOut']
type State = components['schemas']['StateOut']
type Tag = components['schemas']['TagOut']

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
  const [priority, setPriority] = useState(3)
  const [stateId, setStateId] = useState('')
  const [tagId, setTagId] = useState('')
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)

  const apply = useCallback((tk: Task) => {
    setTask(tk)
    setTitle(tk.title)
    setDescription(tk.description ?? '')
    setPriority(tk.priority)
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

  useEffect(() => {
    let active = true
    void (async () => {
      const h = workspaceHeader()
      const [tk, st, tg] = await Promise.all([
        api.GET('/tasks/{task_id}', { params: { header: h, path: { task_id: id } } }),
        api.GET('/tasks/{task_id}/states', {
          params: { header: h, path: { task_id: id } },
        }),
        api.GET('/tags', { params: { header: h } }),
      ])
      if (!active) return
      if (tk.data) apply(tk.data)
      else setErr(errMessage(tk.error))
      if (st.data) setStates(st.data)
      if (tg.data) setTags(tg.data)
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
        priority,
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
          <textarea
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
        </label>
        <label>
          {t('tasks.priority')}
          <input
            type="number"
            min={1}
            max={5}
            value={priority}
            onChange={(e) => setPriority(Number(e.target.value))}
          />
        </label>
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
    </section>
  )
}
