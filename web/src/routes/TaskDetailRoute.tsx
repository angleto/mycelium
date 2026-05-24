import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import { RichEditor } from '../components/RichEditor'
import { AssigneePicker } from '../components/AssigneePicker'
import { OwnerPicker } from '../components/OwnerPicker'
import { TagPicker } from '../components/TagPicker'
import { PriorityChip } from '../components/PriorityChip'
import { IdentityBadge } from '../components/IdentityBadge'
import { ScaleSelect } from '../components/ScaleSelect'
import { Attachments } from '../components/Attachments'
import { AgentRunPanel } from '../components/AgentRunPanel'
import { CoordinationPanel } from '../components/CoordinationPanel'
import { TaskTimer } from '../components/TaskTimer'
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
type Rel = components['schemas']['TaskRelationOut']

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
  const [rels, setRels] = useState<Rel[]>([])
  const [relQuery, setRelQuery] = useState('')
  const [relOpen, setRelOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [workNotes, setWorkNotes] = useState<
    { id: string; title: string | null }[]
  >([])

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

  const reloadRels = useCallback(async () => {
    const { data } = await api.GET('/task-relations', {
      params: { header: workspaceHeader(), query: { task_id: id } },
    })
    if (data) setRels(data)
  }, [id])

  useEffect(() => {
    let active = true
    void (async () => {
      const h = workspaceHeader()
      const [tk, st, tg, all, dp, ws, rm, nt, rl] = await Promise.all([
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
        api.GET('/notes', { params: { header: h } }),
        api.GET('/task-relations', { params: { header: h, query: { task_id: id } } }),
      ])
      if (!active) return
      if (tk.data) apply(tk.data)
      else setErr(errMessage(tk.error))
      if (st.data) setStates(st.data)
      if (tg.data) setTags(tg.data)
      if (all.data) setAllTasks(all.data)
      if (dp.data) setDeps(dp.data)
      if (rl.data) setRels(rl.data)
      if (ws.data) setPresets(ws.data.settings?.estimate_presets ?? [])
      if (rm.data) setReminders(rm.data)
      if (nt.data)
        setWorkNotes(
          nt.data
            .filter((n) => n.task_id === id)
            .map((n) => ({ id: n.id, title: n.title })),
        )
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

  async function onNewChild() {
    if (!task) return
    setErr(null)
    // Inherit ALL tags from the parent (client, project, generic).
    // Backend dedups and auto-attaches the client tag from any
    // project tag (#20); generic tags carry over for free. Title is
    // a placeholder; the detail surface (autosave on title change)
    // lets the user rename + set every other field inline.
    const inheritedTagIds = (task.tags ?? []).map((g) => g.id)
    const { data, error } = await api.POST('/tasks', {
      params: { header: workspaceHeader() },
      body: {
        title: t('tasks.newChildPlaceholder'),
        priority: task.priority ?? 3,
        executor_kind: 'human',
        necessity: 'should',
        parent_task_id: task.id,
        tag_ids: inheritedTagIds,
      },
    })
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    navigate(`/tasks/${data.id}`)
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
    // Merge the patch keys into the local task so ``dirty`` clears
    // and the debounced text-autosave effect short-circuits on the
    // next render. Just bumping ``version`` (as before) left
    // task.title pointing at the pre-save value, so dirty stayed
    // true forever and the user saw "unsaved" with no way to clear.
    setTask((p) => {
      if (!p) return p
      const next: typeof p = { ...p, version: data.version }
      for (const [k, v] of Object.entries(patch)) {
        // The PATCH body keys are a strict subset of TaskOut keys
        // (``_UPDATABLE`` in the backend service); cast through Record
        // for the type-system, the runtime stays a 1:1 copy.
        ;(next as unknown as Record<string, unknown>)[k] = v
      }
      return next
    })
  }

  // Title + description autosave: debounce 1s after the last change.
  // Skips the initial mount (no patch for unchanged values) and bails
  // if a sibling autosave already bumped the version while we were
  // typing — the optimistic-concurrency reload triggers, and the
  // debounced patch refires with the fresh version next tick.
  const lastSentText = useRef<{ title: string; description: string } | null>(
    null,
  )
  useEffect(() => {
    if (!task) return
    if (lastSentText.current === null) {
      lastSentText.current = {
        title: task.title,
        description: task.description ?? '',
      }
      return
    }
    const prev = lastSentText.current
    const curDescr = description ?? ''
    if (prev.title === title && prev.description === curDescr) return
    const handle = window.setTimeout(() => {
      const patch: Record<string, unknown> = {}
      if (title !== prev.title) patch.title = title
      if (curDescr !== prev.description) patch.description = curDescr || null
      if (Object.keys(patch).length === 0) return
      lastSentText.current = { title, description: curDescr }
      void autosave(patch)
    }, 1000)
    return () => window.clearTimeout(handle)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [title, description, task?.version])

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

  // Create a fresh work note already linked to this task and jump
  // into it (time logged there is billed to this task).
  async function newWorkNote() {
    const { data, error } = await api.POST('/tasks/{task_id}/notes', {
      params: { header: workspaceHeader(), path: { task_id: id } },
      body: { title: title || t('notes.untitled') },
    })
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    navigate(`/notes?open=${data.id}`)
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

  async function onAddRelated(otherId: string) {
    if (!otherId || otherId === id) return
    setErr(null)
    const { error } = await api.POST('/task-relations', {
      params: { header: workspaceHeader() },
      body: { task_id: id, other_id: otherId },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    setRelQuery('')
    setRelOpen(false)
    await reloadRels()
  }

  async function onRemoveRelated(relId: string) {
    setErr(null)
    const { error } = await api.DELETE('/task-relations/{relation_id}', {
      params: { header: workspaceHeader(), path: { relation_id: relId } },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    await reloadRels()
  }

  const titleOf = (tid: string) =>
    allTasks.find((x) => x.id === tid)?.title ?? tid.slice(0, 8)
  const subtasks = allTasks
    .filter((x) => x.parent_task_id === id && !x.deleted_at)
    .sort((a, b) => a.title.localeCompare(b.title))
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
  // Symmetric relations: pick the *other* endpoint regardless of which
  // side of the canonical pair this task lives on.
  const relatedIds = new Set(
    rels.map((r) => (r.task_a_id === id ? r.task_b_id : r.task_a_id)),
  )
  const relQ = relQuery.trim().toLowerCase()
  const relMatches = allTasks
    .filter(
      (x) =>
        x.id !== id &&
        !relatedIds.has(x.id) &&
        (relQ === '' ||
          x.title.toLowerCase().includes(relQ) ||
          x.id.startsWith(relQ)),
    )
    .slice(0, 10)

  if (err && !task) return <p className="err">{err}</p>
  if (!task) return <p>{t('tasks.loading')}</p>

  // Autosave covers every field. A "save now" button is intentionally
  // gone — the debounced effect above flushes title/description ~1s
  // after the user stops typing, and onSave is kept only as the form's
  // onSubmit fallback (Enter in the title input still saves
  // immediately instead of waiting for the debounce).
  const dirty =
    title !== task.title || description !== (task.description ?? '')

  return (
    <section className="card">
      <div className="taskdetail__top">
        <p className="hint">
          <Link to="/tasks">{t('tasks.back')}</Link>
        </p>
        {/* State select sits immediately left of the timer: the two
            most-used actions on this surface (advance the state,
            start/stop the clock) are now adjacent in the same top row
            instead of buried below the form. Server-authoritative
            timer stays in sync with the work-notes timer below. The
            visible "State" caption is dropped — the dropdown's options
            (the state names themselves) already identify the control,
            so the aria-label carries the semantic for screen readers. */}
        <div className="taskdetail__topright">
          <select
            aria-label={t('tasks.state')}
            value={stateId}
            onChange={(e) => void onChangeState(e.target.value)}
          >
            {states.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </select>
          <TaskTimer taskId={id} />
        </div>
      </div>
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
      {task.assignee_kind ? (
        <p>
          <IdentityBadge
            kind={task.assignee_kind}
            handle={task.assignee_handle ?? null}
          />
        </p>
      ) : task.created_by_kind === 'ai_assistant' ||
        task.created_by_kind === 'mcp_token' ? (
        <p>
          <IdentityBadge
            kind={task.created_by_kind}
            handle={task.created_by_handle ?? null}
            label={task.created_by_label ?? null}
            title={t('tasks.aiCreatedTitle', {
              handle:
                task.created_by_label ?? task.created_by_handle ?? '',
            })}
          />
        </p>
      ) : task.executor_kind === 'llm_agent' ? (
        <p>
          <span className="aibadge" title={t('tasks.aiTitle')}>
            {t('tasks.aiBadge')}
          </span>
        </p>
      ) : null}
      <form onSubmit={(e) => void onSave(e)}>
        <label>
          {t('tasks.newTitle')}
          <input
            required
            value={title}
            onChange={(e) => setTitle(e.target.value)}
          />
        </label>
        <div className="field">
          {t('tasks.description')}
          <RichEditor value={description} onChange={setDescription} />
        </div>
        <div className="row">
          <button type="submit" disabled={busy || !dirty}>
            {busy ? t('tasks.saving') : t('tasks.save')}
          </button>
          {dirty && <span className="muted">{t('tasks.unsaved')}</span>}
          {!dirty && !busy && (
            <span className="muted">{t('tasks.saved')}</span>
          )}
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
        <fieldset className="taskdetail__assignee">
          <legend>{t('assigneePicker.label')}</legend>
          <AssigneePicker
            value={task.assignee_handle ?? null}
            onChange={(next) => void autosave({ assignee_handle: next ?? '' })}
          />
        </fieldset>
        <fieldset className="taskdetail__owner">
          <legend>{t('tasks.ownerLabel')}</legend>
          <p className="hint">{t('tasks.ownerHint')}</p>
          <OwnerPicker
            value={task.owner_id ?? null}
            onChange={(nextOwnerId) =>
              void autosave({ owner_id: nextOwnerId })
            }
          />
        </fieldset>
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
            className="btn--sm"
            onClick={() => void onNewChild()}
            title={t('tasks.newChildHint')}
          >
            {t('tasks.newChild')}
          </button>
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
              <strong>{t('tasks.dependsOn')}:</strong>{' '}
              <Link to={`/tasks/${d.predecessor_id}`}>
                {titleOf(d.predecessor_id)}
              </Link>
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
              <strong>{t('tasks.blocksL')}:</strong>{' '}
              <Link to={`/tasks/${d.successor_id}`}>
                {titleOf(d.successor_id)}
              </Link>
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

      {(task.parent_task_id || subtasks.length > 0) && (
        <>
          <h2>{t('tasks.subtasks')}</h2>
          {task.parent_task_id && (
            <p className="hint">
              {t('tasks.parentLabel')}{' '}
              <Link to={`/tasks/${task.parent_task_id}`}>
                {titleOf(task.parent_task_id)}
              </Link>
            </p>
          )}
          {subtasks.length === 0 ? (
            <p className="hint">{t('tasks.subtasksNone')}</p>
          ) : (
            <ul className="subtasks">
              {subtasks.map((s) => (
                <li key={s.id}>
                  <Link to={`/tasks/${s.id}`}>{s.title}</Link>
                  {s.is_archived && (
                    <span className="hint"> ({t('tasks.archivedSuffix')})</span>
                  )}
                </li>
              ))}
            </ul>
          )}
        </>
      )}

      <h2>{t('tasks.related')}</h2>
      {rels.length === 0 ? (
        <p className="hint">{t('tasks.relatedNone')}</p>
      ) : (
        <div className="chips">
          {rels.map((r) => {
            const otherId = r.task_a_id === id ? r.task_b_id : r.task_a_id
            return (
              <span key={r.id} className="chip">
                <Link to={`/tasks/${otherId}`}>{titleOf(otherId)}</Link>
                <button
                  type="button"
                  className="btn--ghost btn--sm"
                  title={t('tasks.relatedRemove')}
                  onClick={() => void onRemoveRelated(r.id)}
                >
                  ✕
                </button>
              </span>
            )
          })}
        </div>
      )}
      <div className="row">
        <label className="deppick">
          {t('tasks.relatedAdd')}
          <input
            type="text"
            value={relQuery}
            placeholder={t('tasks.relatedSearch')}
            onChange={(e) => {
              setRelQuery(e.target.value)
              setRelOpen(true)
            }}
            onFocus={() => setRelOpen(true)}
            onBlur={() => window.setTimeout(() => setRelOpen(false), 150)}
          />
          {relOpen && relMatches.length > 0 && (
            <div className="deppick__list">
              {relMatches.map((x) => (
                <button
                  key={x.id}
                  type="button"
                  className="deppick__row"
                  onMouseDown={(e) => {
                    e.preventDefault()
                    void onAddRelated(x.id)
                  }}
                >
                  {x.title}
                </button>
              ))}
            </div>
          )}
        </label>
      </div>

      <h2>{t('coord.title')}</h2>
      <CoordinationPanel
        taskId={id}
        offered={task.offered}
        titleOf={titleOf}
        onChanged={() => void reload()}
      />

      <h2>{t('tasks.workNotes')}</h2>
      {/* The TaskTimer at the top of this view (next to the state
          select) is the canonical timer for the task; the section-
          level ⏱▶ / ⏱▶▶ buttons that used to sit here read as
          unrelated to "Work notes" and confused users into thinking
          they started a per-note timer — they didn't. Removed in the
          UX pass; the per-note timer (inside RichEditor's note shell)
          still carries the note_id provenance. */}
      <div className="row">
        <button type="button" onClick={() => void newWorkNote()}>
          {t('tasks.newWorkNote')}
        </button>
      </div>
      {workNotes.length === 0 ? (
        <p className="hint">{t('tasks.noWorkNotes')}</p>
      ) : (
        <ul className="list">
          {workNotes.map((n) => (
            <li key={n.id}>
              <Link to={`/notes?open=${n.id}`}>
                {n.title || t('notes.untitled')}
              </Link>
            </li>
          ))}
        </ul>
      )}

      <h2>{t('attach.title')}</h2>
      <Attachments taskId={id} />

      {task.executor_kind === 'llm_agent' && (
        <>
          <h2>{t('agentrun.title')}</h2>
          <AgentRunPanel taskId={id} />
        </>
      )}
    </section>
  )
}
