import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import { RichEditor } from '../components/RichEditor'
import { TagPicker } from '../components/TagPicker'
import { PriorityChip } from '../components/PriorityChip'
import { ScaleSelect } from '../components/ScaleSelect'
import { formatHours } from '../lib/estimate'

// Mirrors backend derive_priority. importance/urgency are 1..5 where
// 1 = most pressing (Critical / Now); priority = importance*urgency,
// 1 (Critical+Now) .. 25 (Trivial+Whenever).
function derivePriority(imp: number, urg: number): number {
  return Math.max(1, Math.min(25, imp * urg))
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
  const navigate = useNavigate()
  const { id = '' } = useParams()
  const [task, setTask] = useState<Task | null>(null)
  const [states, setStates] = useState<State[]>([])
  const [tags, setTags] = useState<Tag[]>([])
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [importance, setImportance] = useState(4)
  const [urgency, setUrgency] = useState(4)
  const [estimate, setEstimate] = useState('')
  const [due, setDue] = useState('')
  // '' = inherit project default, 'yes' = billable, 'no' = not.
  const [bill, setBill] = useState<'' | 'yes' | 'no'>('')
  const [estCustom, setEstCustom] = useState(false)
  const [presets, setPresets] = useState<string[]>([])
  const [stateId, setStateId] = useState('')
  const [allTasks, setAllTasks] = useState<Task[]>([])
  const [reminders, setReminders] = useState<
    components['schemas']['ReminderOut'][]
  >([])
  const [remOff, setRemOff] = useState('1440')
  const [deps, setDeps] = useState<Dep[]>([])
  const [depOther, setDepOther] = useState('')
  const [depQuery, setDepQuery] = useState('')
  const [depOpen, setDepOpen] = useState(false)
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
    setEstimate(tk.estimate_effort_h ?? '')
    setDue(tk.due_date ?? '')
    setBill(tk.billable == null ? '' : tk.billable ? 'yes' : 'no')
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
      const [tk, st, tg, all, dp, ws, rm] = await Promise.all([
        api.GET('/tasks/{task_id}', { params: { header: h, path: { task_id: id } } }),
        api.GET('/tasks/{task_id}/states', {
          params: { header: h, path: { task_id: id } },
        }),
        api.GET('/tags', { params: { header: h } }),
        api.GET('/tasks', { params: { header: h } }),
        api.GET('/dependencies', { params: { header: h } }),
        api.GET('/workspaces/me', { params: { header: h } }),
        api.GET('/tasks/{task_id}/reminders', {
          params: { header: h, path: { task_id: id } },
        }),
      ])
      if (!active) return
      if (tk.data) apply(tk.data)
      else setErr(errMessage(tk.error))
      if (st.data) setStates(st.data)
      if (tg.data) setTags(tg.data)
      if (all.data) setAllTasks(all.data)
      if (dp.data) setDeps(dp.data)
      if (ws.data) setPresets(ws.data.settings?.estimate_presets ?? [])
      if (rm.data) setReminders(rm.data)
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

  async function onDelete() {
    if (!task) return
    if (!window.confirm(t('tasks.confirmDelete', { title: task.title }))) return
    setErr(null)
    const { error } = await api.POST('/tasks/{task_id}/delete', {
      params: { header: workspaceHeader(), path: { task_id: id } },
      body: { expected_version: task.version },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    navigate('/tasks')
  }

  async function onArchive() {
    if (!task) return
    setErr(null)
    const { error } = await api.POST('/tasks/{task_id}/archive', {
      params: { header: workspaceHeader(), path: { task_id: id } },
      body: { expected_version: task.version },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    navigate('/tasks')
  }

  async function onRestore() {
    if (!task) return
    setErr(null)
    const { error } = await api.POST('/tasks/{task_id}/restore', {
      params: { header: workspaceHeader(), path: { task_id: id } },
      body: { expected_version: task.version },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    await reload()
  }

  async function onUnarchive() {
    if (!task) return
    setErr(null)
    const { error } = await api.POST('/tasks/{task_id}/unarchive', {
      params: { header: workspaceHeader(), path: { task_id: id } },
      body: { expected_version: task.version },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    await reload()
  }

  // Auto-save (no Save button) for the non-text fields: importance/
  // urgency (backend re-derives priority), estimate, due. On success
  // we only bump the local version — never reload(), which would
  // clobber an unsaved title/description edit.
  async function autosave(patch: Record<string, unknown>) {
    if (!task) return
    setErr(null)
    const { data, error, response } = await api.PATCH('/tasks/{task_id}', {
      params: { header: workspaceHeader(), path: { task_id: id } },
      body: { expected_version: task.version, ...patch },
    })
    if (response.status === 409) {
      setErr(t('tasks.conflict'))
      await reload()
      return
    }
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    setTask((p) => (p ? { ...p, version: data.version } : p))
  }

  function onImp(n: number) {
    setImportance(n)
    void autosave({ importance: n, urgency })
  }
  function onUrg(n: number) {
    setUrgency(n)
    void autosave({ importance, urgency: n })
  }
  function onDue(v: string) {
    setDue(v)
    void autosave({ due_date: v || null })
  }
  function commitEstimate(v: string) {
    void autosave({ estimate_effort_h: v.trim() ? v.trim() : null })
  }
  function onBill(v: '' | 'yes' | 'no') {
    setBill(v)
    void autosave({ billable: v === '' ? null : v === 'yes' })
  }

  async function reloadReminders() {
    const { data } = await api.GET('/tasks/{task_id}/reminders', {
      params: { header: workspaceHeader(), path: { task_id: id } },
    })
    if (data) setReminders(data)
  }

  async function addReminder() {
    setErr(null)
    const { error } = await api.POST('/tasks/{task_id}/reminders', {
      params: { header: workspaceHeader(), path: { task_id: id } },
      body: { offset_minutes: Number(remOff) },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    await reloadReminders()
  }

  async function removeReminder(rid: string) {
    setErr(null)
    const { error } = await api.DELETE(
      '/tasks/{task_id}/reminders/{reminder_id}',
      {
        params: {
          header: workspaceHeader(),
          path: { task_id: id, reminder_id: rid },
        },
      },
    )
    if (error) {
      setErr(errMessage(error))
      return
    }
    await reloadReminders()
  }

  function fmtOffset(m: number): string {
    if (m === 0) return t('tasks.remAtDue')
    if (m % 10080 === 0) return t('tasks.remBefore', { v: `${m / 10080}w` })
    if (m % 1440 === 0) return t('tasks.remBefore', { v: `${m / 1440}d` })
    if (m % 60 === 0) return t('tasks.remBefore', { v: `${m / 60}h` })
    return t('tasks.remBefore', { v: `${m}m` })
  }

  async function onChangeState(sid: string) {
    if (!task) return
    setStateId(sid)
    setErr(null)
    const { error, response } = await api.POST('/tasks/{task_id}/state', {
      params: { header: workspaceHeader(), path: { task_id: id } },
      body: { expected_version: task.version, state_id: sid },
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

  async function addTag(tagId: string) {
    setErr(null)
    const { error } = await api.POST('/tasks/{task_id}/tags', {
      params: { header: workspaceHeader(), path: { task_id: id } },
      body: { tag_id: tagId },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    await reload()
  }

  async function removeTag(tagId: string) {
    setErr(null)
    const { error } = await api.DELETE('/tasks/{task_id}/tags/{tag_id}', {
      params: {
        header: workspaceHeader(),
        path: { task_id: id, tag_id: tagId },
      },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    await reload()
  }

  // Open (creating on first use) this task's work note. Writing in it
  // is billable: the note is linked to the task, so its timer feeds
  // the task → project → client.
  async function openWorkNote() {
    setErr(null)
    const { data, error } = await api.POST('/tasks/{task_id}/note', {
      params: { header: workspaceHeader(), path: { task_id: id } },
    })
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    navigate(`/notes?open=${data.id}`)
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
    setDepQuery('')
    setDepOpen(false)
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
  const depQ = depQuery.trim().toLowerCase()
  const depMatches = allTasks
    .filter(
      (x) =>
        x.id !== id &&
        (depQ === '' ||
          x.title.toLowerCase().includes(depQ) ||
          x.id.startsWith(depQ)),
    )
    .slice(0, 10)

  if (err && !task) return <p className="err">{err}</p>
  if (!task) return <p>{t('tasks.loading')}</p>

  // Only title/description go through Save; everything else autosaves.
  const dirty =
    title !== task.title || description !== (task.description ?? '')

  return (
    <section className="card">
      <p className="hint">
        <Link to="/tasks">{t('tasks.back')}</Link>
      </p>
      {task.deleted_at != null && (
        <p className="banner">
          {t('trash.deleted')}
          <button type="button" className="btn--sm" onClick={() => void onRestore()}>
            {t('trash.undelete')}
          </button>
        </p>
      )}
      {task.deleted_at == null && task.is_archived && (
        <p className="banner">
          {t('trash.archived')}
          <button type="button" className="btn--sm" onClick={() => void onUnarchive()}>
            {t('trash.unarchive')}
          </button>
        </p>
      )}
      {task.executor_kind === 'llm_agent' && (
        <p>
          <span className="aibadge" title={t('tasks.aiTitle')}>
            {t('tasks.aiBadge')}
          </span>
        </p>
      )}
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
          <button type="submit" disabled={busy || !dirty}>
            {busy ? t('tasks.saving') : t('tasks.save')}
          </button>
          {dirty && <span className="muted">{t('tasks.unsaved')}</span>}
        </div>
        <div className="row">
          <label>
            {t('tasks.importance')}
            <ScaleSelect value={importance} onChange={onImp} labelsKey="tasks.impLabels" />
          </label>
          <label>
            {t('tasks.urgency')}
            <ScaleSelect value={urgency} onChange={onUrg} labelsKey="tasks.urgLabels" />
          </label>
          <PriorityChip
            priority={derivePriority(importance, urgency)}
            score={importance * urgency}
          />
        </div>
        <label>
          {t('tasks.due')}
          <input
            type="date"
            value={due}
            onChange={(e) => onDue(e.target.value)}
          />
        </label>
        <label>
          {t('tasks.billable')}
          <select
            value={bill}
            onChange={(e) => onBill(e.target.value as '' | 'yes' | 'no')}
          >
            <option value="">{t('tasks.billInherit')}</option>
            <option value="yes">{t('tasks.billYes')}</option>
            <option value="no">{t('tasks.billNo')}</option>
          </select>
        </label>
        <div>
          <strong>{t('tasks.reminders')}</strong>{' '}
          <span className="hint">{t('tasks.remHint')}</span>
          <div className="row" style={{ flexWrap: 'wrap' }}>
            {reminders.map((r) => (
              <span key={r.id} className="chip">
                {fmtOffset(r.offset_minutes)}
                <button
                  type="button"
                  className="btn--ghost btn--sm"
                  onClick={() => void removeReminder(r.id)}
                >
                  ✕
                </button>
              </span>
            ))}
          </div>
          <div className="row">
            <select
              value={remOff}
              onChange={(e) => setRemOff(e.target.value)}
            >
              <option value="0">{t('tasks.remAtDue')}</option>
              <option value="60">{t('tasks.remBefore', { v: '1h' })}</option>
              <option value="240">{t('tasks.remBefore', { v: '4h' })}</option>
              <option value="1440">{t('tasks.remBefore', { v: '1d' })}</option>
              <option value="2880">{t('tasks.remBefore', { v: '2d' })}</option>
              <option value="10080">{t('tasks.remBefore', { v: '1w' })}</option>
            </select>
            <button
              type="button"
              className="btn--sm"
              onClick={() => void addReminder()}
            >
              {t('tasks.remAdd')}
            </button>
          </div>
        </div>
        {(() => {
          const isPreset =
            estimate.trim() !== '' &&
            !estCustom &&
            presets.some((p) => Number(p) === Number(estimate))
          const selVal = isPreset ? String(Number(estimate)) : 'custom'
          return (
            <label>
              {t('tasks.estimate')}
              <span className="row">
                <select
                  className="est__sel"
                  value={selVal}
                  onChange={(e) => {
                    const v = e.target.value
                    if (v === 'custom') {
                      setEstCustom(true)
                    } else {
                      setEstimate(v)
                      setEstCustom(false)
                      commitEstimate(v)
                    }
                  }}
                >
                  {presets.map((p) => (
                    <option key={p} value={String(Number(p))}>
                      {formatHours(Number(p))}
                    </option>
                  ))}
                  <option value="custom">{t('tasks.estCustom')}</option>
                </select>
                {!isPreset && (
                  <input
                    type="number"
                    min={0}
                    step="0.25"
                    placeholder={t('tasks.estPlaceholder')}
                    value={estimate}
                    onChange={(e) => setEstimate(e.target.value)}
                    onBlur={(e) => commitEstimate(e.target.value)}
                  />
                )}
              </span>
            </label>
          )
        })()}
        {msg && <p className="ok">{msg}</p>}
        {err && <p className="err">{err}</p>}
        <div className="row">
          <button
            type="button"
            className="btn--ghost btn--sm"
            onClick={() => void onArchive()}
          >
            {t('tasks.archive')}
          </button>
          <button
            type="button"
            className="btn--danger btn--sm"
            onClick={() => void onDelete()}
          >
            {t('tasks.delete')}
          </button>
        </div>
      </form>

      <div className="row">
        <label>
          {t('tasks.state')}
          <select
            value={stateId}
            onChange={(e) => void onChangeState(e.target.value)}
          >
            {states.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </select>
        </label>
      </div>

      <div className="row">
        <button type="button" onClick={() => void openWorkNote()}>
          {t('tasks.workNote')}
        </button>
      </div>

      <h2>{t('tasks.tagsTitle')}</h2>
      <TagPicker
        selected={task?.tags ?? []}
        all={tags}
        onAdd={(tid) => void addTag(tid)}
        onRemove={(tid) => void removeTag(tid)}
      />

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
        <label className="deppick">
          {t('tasks.otherTask')}
          <input
            type="text"
            value={depQuery}
            placeholder={t('tasks.depSearch')}
            onChange={(e) => {
              setDepQuery(e.target.value)
              setDepOther('')
              setDepOpen(true)
            }}
            onFocus={() => setDepOpen(true)}
            onBlur={() => window.setTimeout(() => setDepOpen(false), 150)}
          />
          {depOpen && depMatches.length > 0 && (
            <div className="deppick__list">
              {depMatches.map((x) => (
                <button
                  key={x.id}
                  type="button"
                  className={
                    'deppick__row' +
                    (x.id === depOther ? ' deppick__row--sel' : '')
                  }
                  onMouseDown={(e) => {
                    e.preventDefault()
                    setDepOther(x.id)
                    setDepQuery(x.title)
                    setDepOpen(false)
                  }}
                >
                  {x.title}
                </button>
              ))}
            </div>
          )}
        </label>
        <button type="button" onClick={() => void onAddDep()}>
          {t('tasks.addDep')}
        </button>
      </div>
      <p className="hint">{t('tasks.relatedTo')}</p>
    </section>
  )
}
