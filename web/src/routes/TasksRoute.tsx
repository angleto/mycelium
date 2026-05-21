import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import { useSession } from '../auth/useSession'
import { TagChip } from '../components/TagChip'
import { PriorityChip } from '../components/PriorityChip'
import { ScaleSelect } from '../components/ScaleSelect'
import { TaskKanban } from '../components/TaskKanban'
import { useFocus } from '../lib/focus'
import { useLinkedClientProject } from '../lib/linkedClientProject'
import type { components } from '../api/schema'

type View = 'kanban' | 'list'
const VIEW_KEY = 'flow.tasks.view'

// Default view per viewport: desktop favours the kanban board, mobile
// favours the dense list (small screens can't fit multiple columns
// side-by-side legibly). Once the user picks a view it's persisted, so
// subsequent visits respect their choice across viewports.
function defaultView(): View {
  try {
    const saved = localStorage.getItem(VIEW_KEY)
    if (saved === 'kanban' || saved === 'list') return saved
  } catch {
    /* private mode / quota: fall through to viewport default */
  }
  if (typeof window !== 'undefined' && window.matchMedia) {
    return window.matchMedia('(max-width: 768px)').matches ? 'list' : 'kanban'
  }
  return 'kanban'
}

type Task = components['schemas']['TaskOut']
type Tag = components['schemas']['TagOut']
type Project = components['schemas']['ProjectOut']
type Running = components['schemas']['TimeEntryOut']
type State = components['schemas']['StateOut']
type Wf = components['schemas']['WorkflowOut']

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
  const { focusIds, active: focusActive } = useFocus()
  const [tasks, setTasks] = useState<Task[]>([])
  const [tags, setTags] = useState<Tag[]>([])
  // Projects come from /projects (not /tags) because only ProjectOut
  // carries client_tag_id, which we need to (a) filter the Project
  // dropdown by selected client and (b) auto-set Client when the user
  // picks a Project.
  const [projectsByClient, setProjectsByClient] = useState<Project[]>([])
  const [filter, setFilter] = useState('')
  const [title, setTitle] = useState('')
  // Default Low/Low (value 4 on the 1=Critical..5=Trivial scale).
  const [importance, setImportance] = useState(4)
  const [urgency, setUrgency] = useState(4)
  const [due, setDue] = useState('')
  const [q, setQ] = useState('')
  const [running, setRunning] = useState<Running[]>([])
  const [now, setNow] = useState<number>(() => Date.now())
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  // Workflow meta for inline + bulk state changes. Tasks share the
  // org default workflow (no project-override UI yet); a task whose
  // state is outside it degrades to read-only text.
  const [wfStates, setWfStates] = useState<State[]>([])
  const [edges, setEdges] = useState<Array<{ from: string; to: string }>>([])
  const [sel, setSel] = useState<Set<string>>(new Set())
  const [bulkState, setBulkState] = useState('')
  const [bulkTag, setBulkTag] = useState('')
  const [bulkMsg, setBulkMsg] = useState<string | null>(null)
  // Kanban hides columns whose state has ``is_hidden=true`` by default
  // (per-state UI hint set in /workflows). The list view always shows
  // everything: ``showTerminal`` was removed because terminal tasks are
  // legitimate "done" rows the user wants to see in their list — only
  // the graph still hides them by default (it has its own toggle).
  const [showHidden, setShowHidden] = useState(false)
  const [view, setView] = useState<View>(defaultView)
  useEffect(() => {
    try {
      localStorage.setItem(VIEW_KEY, view)
    } catch {
      /* ignore */
    }
  }, [view])

  const stateById = new Map(wfStates.map((s) => [s.id, s]))
  const allowed = new Map<string, Set<string>>()
  for (const e of edges) {
    if (!allowed.has(e.from)) allowed.set(e.from, new Set())
    allowed.get(e.from)!.add(e.to)
  }

  const {
    clientId,
    projectId,
    onPickClient,
    onPickProject,
    setClientId,
    filterProjectsByClient,
  } = useLinkedClientProject(projectsByClient)

  const clients = tags.filter((x) => x.kind === 'client')
  const projects = filterProjectsByClient(tags.filter((x) => x.kind === 'project'))

  // A task always carries a client tag (and via the project, transitively
  // belongs to it). Pre-select the default "Personal" client (matching
  // the backend's _DEFAULT_CLIENT_NAME) as soon as the list arrives and
  // nothing is selected yet; fall back to the first client if the name
  // ever changes server-side. The empty option in the picker is gone.
  useEffect(() => {
    if (clientId || clients.length === 0) return
    const def = clients.find((c) => c.name === 'Personal') ?? clients[0]
    if (def) setClientId(def.id)
  }, [clientId, clients])

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

  useEffect(() => {
    let active = true
    void (async () => {
      const h = workspaceHeader()
      // /clients is fetched even though we render the picker from
      // /tags, because the endpoint side-effects ``ensure_default_client``
      // — guarantees the "Personal" default tag exists and can therefore
      // be pre-selected (a task must always carry a client tag).
      const [tk, tg, pj] = await Promise.all([
        api.GET('/tasks', {
          params: { header: h, query: filter ? { tag_id: filter } : {} },
        }),
        api.GET('/tags', { params: { header: h } }),
        api.GET('/projects', { params: { header: h } }),
        // /clients is fetched in parallel and intentionally discarded:
        // the endpoint side-effects ``ensure_default_client`` so the
        // "Personal" default tag exists and can be pre-selected (a task
        // must always carry a client tag).
        api.GET('/clients', { params: { header: h } }),
      ])
      if (!active) return
      if (tk.data) setTasks(tk.data)
      else setErr(errMessage(tk.error))
      if (tg.data) setTags(tg.data)
      if (pj.data) setProjectsByClient(pj.data)
    })()
    return () => {
      active = false
    }
  }, [activeId, filter])

  useEffect(() => {
    let active = true
    void (async () => {
      const h = workspaceHeader()
      const wfs = await api.GET('/workflows', { params: { header: h } })
      if (!active || !wfs.data) return
      const def =
        wfs.data.find((w: Wf) => w.is_default) ?? wfs.data[0]
      if (!def) return
      const [st, tr] = await Promise.all([
        api.GET('/workflows/{workflow_id}/states', {
          params: { header: h, path: { workflow_id: def.id } },
        }),
        api.GET('/workflows/{workflow_id}/transitions', {
          params: { header: h, path: { workflow_id: def.id } },
        }),
      ])
      if (!active) return
      if (st.data) setWfStates(st.data)
      if (tr.data)
        setEdges(
          tr.data.map((e) => ({
            from: e.from_state_id,
            to: e.to_state_id,
          })),
        )
    })()
    return () => {
      active = false
    }
  }, [activeId])

  useEffect(() => {
    let active = true
    const tick = async () => {
      const { data } = await api.GET('/time/running', {
        params: { header: workspaceHeader() },
      })
      if (active) setRunning(data ?? [])
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
        // Always emit the client tag (a task is never client-less; the
        // default Personal is pre-selected when nothing is chosen) and
        // the project tag when one is picked. The backend resolves the
        // project anchor from the project-kind tag inside this list.
        tag_ids: [
          ...(clientId ? [clientId] : []),
          ...(projectId ? [projectId] : []),
        ],
        ...(due ? { due_date: due } : {}),
      },
    })
    setBusy(false)
    if (error) {
      setErr(errMessage(error))
      return
    }
    setTitle('')
    setDue('')
    await loadTasks()
  }

  async function toggleTimer(taskId: string) {
    setErr(null)
    const onThis = running.some((r) => r.task_id === taskId)
    const { error } = onThis
      ? await api.POST('/time/stop', {
          params: { header: workspaceHeader() },
          body: { task_id: taskId },
        })
      : await api.POST('/time/start', {
          params: { header: workspaceHeader() },
          body: { task_id: taskId, parallel: false },
        })
    if (error) {
      setErr(errMessage(error))
      return
    }
    const { data } = await api.GET('/time/running', {
      params: { header: workspaceHeader() },
    })
    setRunning(data ?? [])
  }

  async function startParallel(taskId: string) {
    setErr(null)
    const { error } = await api.POST('/time/start', {
      params: { header: workspaceHeader() },
      body: { task_id: taskId, parallel: true },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    const { data } = await api.GET('/time/running', {
      params: { header: workspaceHeader() },
    })
    setRunning(data ?? [])
  }

  function toggleSel(id: string) {
    setSel((s) => {
      const n = new Set(s)
      if (n.has(id)) n.delete(id)
      else n.add(id)
      return n
    })
  }

  async function changeState(tk: Task, toId: string) {
    if (toId === tk.state_id) return
    setErr(null)
    setBulkMsg(null)
    const { error } = await api.POST('/tasks/{task_id}/state', {
      params: { header: workspaceHeader(), path: { task_id: tk.id } },
      body: { expected_version: tk.version, state_id: toId },
    })
    if (error) {
      setErr(errMessage(error))
    }
    await loadTasks()
  }

  // Bulk over the selection. The workflow transition graph is the
  // single source of truth: a target illegal from a task's current
  // state is skipped (never bypassed) and reported, not failed hard.
  async function bulkApplyState() {
    if (!bulkState) return
    setErr(null)
    let applied = 0
    let skipped = 0
    for (const tk of tasks.filter((x) => sel.has(x.id))) {
      if (tk.state_id === bulkState) continue
      if (!allowed.get(tk.state_id)?.has(bulkState)) {
        skipped += 1
        continue
      }
      const { error } = await api.POST('/tasks/{task_id}/state', {
        params: { header: workspaceHeader(), path: { task_id: tk.id } },
        body: { expected_version: tk.version, state_id: bulkState },
      })
      if (error) skipped += 1
      else applied += 1
    }
    setSel(new Set())
    await loadTasks()
    setBulkMsg(t('tasks.bulkResult', { applied, skipped }))
  }

  async function bulkApplyTag(add: boolean) {
    if (!bulkTag) return
    setErr(null)
    for (const tk of tasks.filter((x) => sel.has(x.id))) {
      if (add) {
        await api.POST('/tasks/{task_id}/tags', {
          params: { header: workspaceHeader(), path: { task_id: tk.id } },
          body: { tag_id: bulkTag },
        })
      } else {
        await api.DELETE('/tasks/{task_id}/tags/{tag_id}', {
          params: {
            header: workspaceHeader(),
            path: { task_id: tk.id, tag_id: bulkTag },
          },
        })
      }
    }
    setSel(new Set())
    await loadTasks()
    setBulkMsg(t('tasks.bulkTagDone'))
  }

  async function bulkDelete() {
    const picked = tasks.filter((x) => sel.has(x.id))
    if (picked.length === 0) return
    if (!window.confirm(t('tasks.confirmDeleteN', { n: picked.length }))) return
    setErr(null)
    let done = 0
    for (const tk of picked) {
      const { error } = await api.POST('/tasks/{task_id}/delete', {
        params: { header: workspaceHeader(), path: { task_id: tk.id } },
        body: { expected_version: tk.version },
      })
      if (!error) done += 1
    }
    setSel(new Set())
    await loadTasks()
    setBulkMsg(t('tasks.bulkResult', { applied: done, skipped: picked.length - done }))
  }

  async function bulkArchive() {
    const picked = tasks.filter((x) => sel.has(x.id))
    if (picked.length === 0) return
    setErr(null)
    let done = 0
    for (const tk of picked) {
      const { error } = await api.POST('/tasks/{task_id}/archive', {
        params: { header: workspaceHeader(), path: { task_id: tk.id } },
        body: { expected_version: tk.version },
      })
      if (!error) done += 1
    }
    setSel(new Set())
    await loadTasks()
    setBulkMsg(t('tasks.bulkResult', { applied: done, skipped: picked.length - done }))
  }

  const activeTag = tags.find((x) => x.id === filter)
  const ql = q.trim().toLowerCase()
  const hiddenStateIds = new Set(
    wfStates.filter((s) => s.is_hidden).map((s) => s.id),
  )
  const matched = ql
    ? tasks.filter(
        (tk) =>
          tk.title.toLowerCase().includes(ql) ||
          (tk.tags ?? []).some((g) => g.name.toLowerCase().includes(ql)),
      )
    : tasks
  // Focus (sidebar): client (all its projects) or one project. Only
  // tasks tagged with a focused project show — additive to the filter.
  const focused = focusActive
    ? matched.filter((tk) =>
        (tk.tags ?? []).some((g) => focusIds.includes(g.id)),
      )
    : matched
  // List view shows everything; kanban filters out tasks whose state is
  // hidden unless ``showHidden`` is on (the toggle gates the kanban
  // columns directly inside TaskKanban — see ``kanbanStates``).
  const shown = focused
  const kanbanStates = showHidden
    ? wfStates
    : wfStates.filter((s) => !hiddenStateIds.has(s.id))

  return (
    <section className="card card--wide">
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
          <ScaleSelect
            value={importance}
            onChange={setImportance}
            labelsKey="tasks.impLabels"
          />
        </label>
        <label>
          {t('tasks.urgency')}
          <ScaleSelect
            value={urgency}
            onChange={setUrgency}
            labelsKey="tasks.urgLabels"
          />
        </label>
        <label>
          {t('tasks.due')}
          <input
            type="date"
            value={due}
            onChange={(e) => setDue(e.target.value)}
          />
        </label>
        <label>
          {t('tasks.client')}
          <select
            value={clientId}
            onChange={(e) => onPickClient(e.target.value)}
            required
          >
            {/* Disabled placeholder only while clients are still
                loading; the useEffect above pre-selects the default
                Personal client as soon as the list arrives, so this
                row disappears on first render after load. */}
            {!clientId && (
              <option value="" disabled>
                …
              </option>
            )}
            {clients.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          {t('tasks.project')}
          <select value={projectId} onChange={(e) => onPickProject(e.target.value)}>
            <option value="">{t('tasks.noProject')}</option>
            {projects.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        </label>
        <button type="submit" disabled={busy}>
          {busy ? t('tasks.saving') : t('tasks.create')}
        </button>
      </form>
      <p className="hint">
        {t('tasks.cpManaged')}{' '}
        <Link to="/clients">{t('cp.nav')}</Link>
      </p>

      <div className="row">
        <input
          placeholder={t('tasks.search')}
          value={q}
          onChange={(e) => setQ(e.target.value)}
          style={{ flex: 1, minWidth: '12rem' }}
        />
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
        {view === 'kanban' && (
          <button
            type="button"
            role="switch"
            aria-checked={showHidden}
            className={
              'toggle-pill' + (showHidden ? ' toggle-pill--on' : '')
            }
            onClick={() => setShowHidden((v) => !v)}
          >
            {t('tasks.showHidden')}: {showHidden ? t('common.on') : t('common.off')}
          </button>
        )}
        <div className="viewtabs" role="tablist" aria-label={t('tasks.viewSwitch')}>
          <button
            type="button"
            role="tab"
            aria-selected={view === 'kanban'}
            className={
              'viewtabs__tab' +
              (view === 'kanban' ? ' viewtabs__tab--active' : '')
            }
            onClick={() => setView('kanban')}
          >
            {t('tasks.viewKanban')}
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={view === 'list'}
            className={
              'viewtabs__tab' +
              (view === 'list' ? ' viewtabs__tab--active' : '')
            }
            onClick={() => setView('list')}
          >
            {t('tasks.viewList')}
          </button>
        </div>
      </div>

      {err && <p className="err">{err}</p>}
      {bulkMsg && <p className="ok">{bulkMsg}</p>}

      {shown.length > 0 && (
        <div className="row">
          {(() => {
            const allOn = sel.size > 0 && shown.every((x) => sel.has(x.id))
            return (
              <button
                type="button"
                role="switch"
                aria-checked={allOn}
                className={
                  'toggle-pill' + (allOn ? ' toggle-pill--on' : '')
                }
                onClick={() =>
                  setSel(allOn ? new Set() : new Set(shown.map((x) => x.id)))
                }
              >
                {t('tasks.selectAll')}: {allOn ? t('common.on') : t('common.off')}
              </button>
            )
          })()}
          {sel.size > 0 && (
            <>
              <span className="muted">
                {t('tasks.selected', { n: sel.size })}
              </span>
              <select
                value={bulkState}
                onChange={(e) => setBulkState(e.target.value)}
                aria-label={t('tasks.bulkState')}
              >
                <option value="">{t('tasks.bulkState')}</option>
                {wfStates.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name}
                  </option>
                ))}
              </select>
              <button
                type="button"
                className="btn--sm"
                onClick={() => void bulkApplyState()}
              >
                {t('tasks.bulkApply')}
              </button>
              <select
                value={bulkTag}
                onChange={(e) => setBulkTag(e.target.value)}
                aria-label={t('tasks.filterTag')}
              >
                <option value="">{t('tasks.filterTag')}</option>
                {tags.map((g) => (
                  <option key={g.id} value={g.id}>
                    {g.kind}: {g.name}
                  </option>
                ))}
              </select>
              <button
                type="button"
                className="btn--sm"
                onClick={() => void bulkApplyTag(true)}
              >
                {t('tasks.bulkAddTag')}
              </button>
              <button
                type="button"
                className="btn--ghost btn--sm"
                onClick={() => void bulkApplyTag(false)}
              >
                {t('tasks.bulkRemoveTag')}
              </button>
              <button
                type="button"
                className="btn--ghost btn--sm"
                onClick={() => void bulkArchive()}
              >
                {t('tasks.bulkArchive')}
              </button>
              <button
                type="button"
                className="btn--danger btn--sm"
                onClick={() => void bulkDelete()}
              >
                {t('tasks.bulkDelete')}
              </button>
            </>
          )}
        </div>
      )}

      {view === 'kanban' ? (
        <TaskKanban
          tasks={shown}
          states={kanbanStates}
          allowed={allowed}
          onChangeState={changeState}
        />
      ) : shown.length === 0 ? (
        <p className="hint">{t('tasks.none')}</p>
      ) : (
        <ul className="list tasklist">
          {shown.map((tk) => {
            const cur = running.find((r) => r.task_id === tk.id)
            const onThis = cur != null
            const elapsed = cur
              ? (now - new Date(cur.started_at).getTime()) / 1000
              : 0
            const score =
              tk.importance != null && tk.urgency != null
                ? tk.importance * tk.urgency
                : null
            return (
              <li key={tk.id} className="taskrow">
                <input
                  type="checkbox"
                  className="taskrow__sel"
                  checked={sel.has(tk.id)}
                  onChange={() => toggleSel(tk.id)}
                  aria-label={t('tasks.select')}
                />
                <Link to={`/tasks/${tk.id}`} className="taskrow__title">
                  {tk.executor_kind === 'llm_agent' && (
                    <span className="aibadge" title={t('tasks.aiTitle')}>
                      {t('tasks.aiBadge')}
                    </span>
                  )}
                  {tk.title}
                </Link>
                <span className="taskrow__tags">
                  {(tk.tags ?? []).map((g) => (
                    <TagChip key={g.id} name={g.name} color={g.color} kind={g.kind} />
                  ))}
                </span>
                <span className="taskrow__meta">
                  <span className="taskrow__sep" aria-hidden="true" />
                  {tk.due_date && (
                    <span className="muted" title={t('tasks.due')}>
                      📅 {tk.due_date}
                    </span>
                  )}
                  <PriorityChip priority={tk.priority} score={score} />
                  {stateById.has(tk.state_id) ? (
                    <select
                      className="taskrow__state"
                      value={tk.state_id}
                      onChange={(e) => void changeState(tk, e.target.value)}
                      aria-label={t('tasks.state')}
                    >
                      <option value={tk.state_id}>{tk.state}</option>
                      {[...(allowed.get(tk.state_id) ?? [])]
                        .map((id) => stateById.get(id))
                        .filter((s): s is State => Boolean(s))
                        .map((s) => (
                          <option key={s.id} value={s.id}>
                            {s.name}
                          </option>
                        ))}
                    </select>
                  ) : (
                    <span className="muted">{tk.state}</span>
                  )}
                  <button
                    type="button"
                    className={onThis ? 'btn--sm' : 'btn--ghost btn--sm'}
                    onClick={() => void toggleTimer(tk.id)}
                    title={onThis ? t('tasks.stop') : t('time.startSerial')}
                  >
                    {onThis ? `⏱■ ${hms(elapsed)}` : '⏱▶'}
                  </button>
                  {!onThis && (
                    <button
                      type="button"
                      className="btn--ghost btn--sm"
                      onClick={() => void startParallel(tk.id)}
                      title={t('time.startParallel')}
                    >
                      ⏱▶▶
                    </button>
                  )}
                </span>
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}
