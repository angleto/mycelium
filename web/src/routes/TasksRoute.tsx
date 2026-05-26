import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api, errMessage, searchTasksByText, workspaceHeader } from '../api/client'
import { useSession } from '../auth/useSession'
import { TagChip } from '../components/TagChip'
import { PriorityChip } from '../components/PriorityChip'
import { IdentityBadge } from '../components/IdentityBadge'
import { TaskKanban } from '../components/TaskKanban'
import { RecentTasks } from '../components/RecentTasks'
import { TaskTimer } from '../components/TaskTimer'
import { TagPickerGrid } from '../components/TagPickerGrid'
import { useFocus } from '../lib/focus'
import { useLinkedClientProject } from '../lib/linkedClientProject'
import { getFreeTextTokens, parseFilter } from '../lib/taskFilter'
import type { components } from '../api/schema'

type View = 'kanban' | 'list'
const VIEW_KEY = 'flow.tasks.view'
const SCOPE_KEY = 'flow.tasks.scope'
const DATEFOCUS_KEY = 'flow.tasks.dateFocus'

type Scope = 'all' | 'today' | 'week' | 'month'
const SCOPES: ReadonlyArray<Scope> = ['all', 'today', 'week', 'month'] as const

function defaultScope(): Scope {
  try {
    const v = localStorage.getItem(SCOPE_KEY)
    if (v === 'all' || v === 'today' || v === 'week' || v === 'month') return v
  } catch {
    /* private mode / quota: fall through */
  }
  return 'all'
}

function defaultDateFocus(): boolean {
  try {
    return localStorage.getItem(DATEFOCUS_KEY) === '1'
  } catch {
    return false
  }
}

// Pick the "date" of a task for scope/date-focus filtering. Appointment
// tasks (start_at + duration_minutes) use start_at; reminders / deadline
// tasks use due_date; plain tasks have no date and never match.
function taskDate(tk: {
  start_at?: string | null
  due_date?: string | null
}): Date | null {
  if (tk.start_at) return new Date(tk.start_at)
  if (tk.due_date) {
    // due_date is a YYYY-MM-DD string; anchor at local midnight so the
    // window math below stays in the user's timezone.
    const [y, m, d] = tk.due_date.split('-').map(Number)
    return new Date(y, (m ?? 1) - 1, d ?? 1)
  }
  return null
}

// Inclusive-start, exclusive-end window for the given scope. Week is
// Monday-anchored (Europe convention); month is calendar month.
function scopeWindow(scope: Scope, now: Date): [Date, Date] | null {
  if (scope === 'all') return null
  const start = new Date(now)
  start.setHours(0, 0, 0, 0)
  const end = new Date(start)
  if (scope === 'today') {
    end.setDate(end.getDate() + 1)
  } else if (scope === 'week') {
    const dow = start.getDay() || 7 // Sun=0 -> 7
    start.setDate(start.getDate() - (dow - 1))
    end.setTime(start.getTime())
    end.setDate(end.getDate() + 7)
  } else {
    start.setDate(1)
    end.setTime(start.getTime())
    end.setMonth(end.getMonth() + 1)
  }
  return [start, end]
}

// Default view per viewport. Mobile (≤768px) ALWAYS starts on the
// dense list: a single kanban column on a phone is just a vertical
// scroll with extra chrome — the list does the same in less space. The
// user can still toggle to kanban for the current visit via the
// view-tabs, but the choice is not persisted on mobile (see save
// effect below) so a future visit comes back to list. Desktop respects
// the saved preference and falls back to kanban.
function isMobileViewport(): boolean {
  return (
    typeof window !== 'undefined' &&
    !!window.matchMedia &&
    window.matchMedia('(max-width: 768px)').matches
  )
}
function defaultView(): View {
  if (isMobileViewport()) return 'list'
  try {
    const saved = localStorage.getItem(VIEW_KEY)
    if (saved === 'kanban' || saved === 'list') return saved
  } catch {
    /* private mode / quota: fall through to viewport default */
  }
  return 'kanban'
}

type Task = components['schemas']['TaskOut']
type Tag = components['schemas']['TagOut']
type Project = components['schemas']['ProjectOut']
type State = components['schemas']['StateOut']
type Wf = components['schemas']['WorkflowOut']
type Client = components['schemas']['ClientOut']

// Tasks surface: quick-add (title + due + client/project) with inline
// create; the rows are title-left / actions-right with a colored
// priority chip and a clock-play/clock-stop timer. Importance/urgency
// are set on the detail view --- the quick-add lets the backend apply
// its Low/Low default so the SPA never duplicates a default value.
export function TasksRoute() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const session = useSession()
  const activeId = session?.workspaceId
  const {
    focusIds,
    active: focusActive,
    clientId: focusClientId,
    projectId: focusProjectId,
  } = useFocus()
  const [tasks, setTasks] = useState<Task[]>([])
  const [tags, setTags] = useState<Tag[]>([])
  // Projects come from /projects (not /tags) because only ProjectOut
  // carries client_tag_id, which we need to (a) filter the Project
  // dropdown by selected client and (b) auto-set Client when the user
  // picks a Project.
  const [projectsByClient, setProjectsByClient] = useState<Project[]>([])
  // Client picker is sourced from /clients (not /tags): that endpoint
  // side-effects ensure_default_client, so its response always carries
  // the Personal default. Pre-selecting from it can't lose a race with
  // the parallel /tags load (a brand-new user otherwise saw an empty
  // required client select until they reloaded).
  const [clientsList, setClientsList] = useState<Client[]>([])
  const [filter, setFilter] = useState('')
  const [title, setTitle] = useState('')
  // Eisenhower axes are NOT exposed in quick-add: the backend supplies
  // the Low/Low default (migration 0102), and the policy is that any
  // default lives in the service, not in the SPA. The user picks the
  // axes from the task detail view when needed.
  const [due, setDue] = useState('')
  const [q, setQ] = useState('')
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
  // (per-state UI hint set in /workflows).
  const [showHidden, setShowHidden] = useState(false)
  // List view hides terminal-state tasks (done/cancelled/...) by default
  // so the working list stays focused on open work; a toggle reveals
  // them. Kanban is unaffected (it has its own per-column hiding).
  const [hideTerminal, setHideTerminal] = useState(true)
  const [view, setView] = useState<View>(defaultView)
  useEffect(() => {
    // Mobile toggling is ephemeral by design (see defaultView): saving
    // a mobile kanban excursion to localStorage would override the
    // user's desktop default the next time they open the app on a
    // laptop, which is not what they asked for.
    if (isMobileViewport()) return
    try {
      localStorage.setItem(VIEW_KEY, view)
    } catch {
      /* ignore */
    }
  }, [view])
  // Scope (All/Today/Week/Month) narrows the visible set by date
  // window; date focus is the orthogonal toggle "only tasks with a
  // date". Both persisted per user via localStorage so the next visit
  // restores the same lens.
  const [scope, setScope] = useState<Scope>(defaultScope)
  const [dateFocus, setDateFocus] = useState<boolean>(defaultDateFocus)
  useEffect(() => {
    try {
      localStorage.setItem(SCOPE_KEY, scope)
    } catch {
      /* ignore */
    }
  }, [scope])
  useEffect(() => {
    try {
      localStorage.setItem(DATEFOCUS_KEY, dateFocus ? '1' : '0')
    } catch {
      /* ignore */
    }
  }, [dateFocus])

  // Server-side task search. The structured DSL stays client-side
  // (instant, no network); the free-text portion of the query goes to
  // /search?kinds=task, which uses the FTS + pgvector RRF pipeline so a
  // matched task whose title doesn't contain the query word (matched
  // via description, checklist text or semantic similarity) shows up.
  // The result is an id-set ANDed into the client-side predicate, so
  // ``state:in_progress checklist-only-word`` filters correctly: the
  // server returns tasks matching the word, the client narrows to
  // state=in_progress without a roundtrip.
  //
  // ``serverIds === null`` means "no free-text in the query" -> no
  // intersection applied (back-compat: structured-only filters work
  // exactly as before). An empty Set means "free-text present, no
  // server hits" -> the list is empty until the user refines.
  const [serverIds, setServerIds] = useState<Set<string> | null>(null)
  const [searching, setSearching] = useState(false)
  useEffect(() => {
    const freeText = getFreeTextTokens(q.trim()).join(' ').trim()
    if (!freeText) {
      // External-state sync (drop stale search results when the user
      // clears the box). The eslint rule fires only on the CI runner
      // for now; disable explicitly so the build is green there.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setServerIds(null)
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setSearching(false)
      return
    }
    const ac = new AbortController()
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setSearching(true)
    // 250ms debounce: fast enough to feel live, slow enough to not
    // flood the embedder on each keystroke.
    const handle = window.setTimeout(() => {
      void (async () => {
        try {
          const hits = await searchTasksByText(freeText, ac.signal)
          if (ac.signal.aborted) return
          setServerIds(
            new Set(
              hits
                .filter((h) => h.kind === 'task' && h.task_id)
                .map((h) => h.task_id as string),
            ),
          )
        } catch {
          if (!ac.signal.aborted) setServerIds(new Set())
        } finally {
          if (!ac.signal.aborted) setSearching(false)
        }
      })()
    }, 250)
    return () => {
      window.clearTimeout(handle)
      ac.abort()
    }
  }, [q])

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
    setProjectId,
    filterProjectsByClient,
  } = useLinkedClientProject(projectsByClient)

  // Clients from /clients (always includes the ensured Personal default);
  // projects still come from /tags filtered by the selected client.
  const clients = clientsList
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

  // Focus-mode sync (task 92a6973e): when the user picks a client
  // and/or project from the Focus sidebar, the Quick add row mirrors
  // that selection so a new task lands inside the focused scope
  // without an extra click. Runs only while focus is active; turning
  // focus off leaves the manual selection alone (no flapping).
  useEffect(() => {
    if (!focusActive) return
    if (focusClientId) setClientId(focusClientId)
    // Project narrowing is optional: when focus is client-only, clear
    // the quick-add project so the new task lands at client level;
    // when focus narrows to a specific project, mirror it.
    setProjectId(focusProjectId || '')
  }, [focusActive, focusClientId, focusProjectId, setClientId, setProjectId])

  const loadTasks = useCallback(async () => {
    setErr(null)
    const { data, error } = await api.GET('/tasks', {
      params: {
        header: workspaceHeader(),
        // include_checklist=true so the free-text filter in
        // lib/taskFilter can match item text (e.g. "pane") on a
        // task whose title is just "Shopping list".
        query: {
          ...(filter ? { tag_id: filter } : {}),
          include_checklist: true,
        },
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
      const [tk, tg, pj, cl] = await Promise.all([
        api.GET('/tasks', {
          params: {
            header: h,
            query: {
              ...(filter ? { tag_id: filter } : {}),
              include_checklist: true,
            },
          },
        }),
        api.GET('/tags', { params: { header: h } }),
        api.GET('/projects', { params: { header: h } }),
        // /clients is the authoritative client list and side-effects
        // ``ensure_default_client``, so its response always includes the
        // Personal default. It drives the client picker + pre-select
        // (clientsList) — see the note on that state.
        api.GET('/clients', { params: { header: h } }),
      ])
      if (!active) return
      if (tk.data) setTasks(tk.data)
      else setErr(errMessage(tk.error))
      if (tg.data) setTags(tg.data)
      if (pj.data) setProjectsByClient(pj.data)
      if (cl.data) setClientsList(cl.data)
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

  async function onCreate(e: FormEvent, openAfter = false) {
    e.preventDefault()
    setBusy(true)
    setErr(null)
    const { data, error } = await api.POST('/tasks', {
      params: { header: workspaceHeader() },
      body: {
        title,
        // Quick-add intentionally omits importance/urgency: the backend
        // defaults to Low/Low (4/4 -> derived priority 16) and that is
        // the single source of truth for the default. ``priority`` is
        // a calculated field and is never an input.
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
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    setTitle('')
    setDue('')
    if (openAfter) {
      // Skip the loadTasks() round-trip: we're leaving /tasks and the
      // detail route doesn't depend on the list cache. Navigation lands
      // straight on the editable surface (description, tags, assignee).
      navigate(`/tasks/${data.id}`)
      return
    }
    await loadTasks()
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

  const hiddenStateIds = new Set(
    wfStates.filter((s) => s.is_hidden).map((s) => s.id),
  )
  // Identity facet (Punto 4, ADR-0028): three-way toggle on top of the
  // ``actor:`` DSL atom — broader than ``executor:`` because it matches
  // the badge predicate on the cards (assignee → AI creator → executor
  // fallback). The previous implementation wrote ``executor:llm_agent``,
  // which filtered on assignee identity only and hid MCP-created tasks
  // that were never assigned to an ai_assistant identity (their
  // executor_kind stays at the human column default), so "Bots"
  // returned an empty list even when the cards showed an AI badge.
  const identityFacet: 'all' | 'humans' | 'bots' = /\bactor:bot\b/.test(q)
    ? 'bots'
    : /\bactor:human\b/.test(q)
      ? 'humans'
      : 'all'
  function setIdentityFacet(f: 'all' | 'humans' | 'bots') {
    const stripped = q
      .replace(/\bactor:(bot|human)\b/g, '')
      .replace(/\s+/g, ' ')
      .trim()
    if (f === 'all') {
      setQ(stripped)
      return
    }
    const atom = f === 'humans' ? 'actor:human' : 'actor:bot'
    setQ(stripped ? `${stripped} ${atom}` : atom)
  }
  // Filter DSL: free text matches title or tag name (existing
  // behaviour); ``@tagname`` / ``state:in_progress`` / ``due:today``
  // / ``priority:<=3`` / ``!@done`` / ``A | B`` cover the Todoist-
  // style structured filters. Parser is in lib/taskFilter.
  const ql = q.trim()
  const matched = ql
    ? (() => {
        const filterCtx = {
          tagsById: new Map(
            tags.map((g) => [g.id, { name: g.name, kind: g.kind ?? 'generic' }]),
          ),
          statesById: new Map(
            wfStates.map((s) => [
              s.id,
              { name: s.name, is_terminal: s.is_terminal },
            ]),
          ),
          now: new Date(),
        }
        const pred = parseFilter(ql, filterCtx)
        // Intersect with the server-side free-text hits. ``serverIds``
        // is null when the query carries no free-text component (only
        // structured atoms), in which case the predicate alone decides
        // -- same behaviour as before this hook landed.
        return tasks.filter(
          (t) => pred(t) && (serverIds === null || serverIds.has(t.id)),
        )
      })()
    : tasks
  // Focus (sidebar): client (all its projects) or one project. Only
  // tasks tagged with a focused project show — additive to the filter.
  const focused = focusActive
    ? matched.filter((tk) =>
        (tk.tags ?? []).some((g) => focusIds.includes(g.id)),
      )
    : matched
  // Date lens (scope + date focus). Two orthogonal axes:
  //   - scope (Today/Week/Month) narrows DATED tasks to the window;
  //   - dateFocus on/off decides whether UNDATED tasks are hidden.
  // So scope=Month + dateFocus=off must show every undated task plus
  // dated tasks whose date falls in the current month (the previous
  // implementation incorrectly hid undated tasks whenever a scope was
  // set, regardless of dateFocus).
  const dateWindow = scopeWindow(scope, new Date())
  const dateLensed =
    !dateWindow && !dateFocus
      ? focused
      : focused.filter((tk) => {
          const d = taskDate(tk)
          if (d === null) {
            // Undated tasks: visible iff date focus is off.
            return !dateFocus
          }
          if (!dateWindow) return true
          return d >= dateWindow[0] && d < dateWindow[1]
        })
  // ``shown`` feeds the kanban (full set; its columns are gated by
  // is_hidden/showHidden). ``listShown`` is the list view's set: in list
  // mode it also drops terminal-state tasks unless ``hideTerminal`` is
  // off. In kanban mode listShown === shown (the gate is view-scoped).
  const shown = dateLensed
  const terminalStateIds = new Set(
    wfStates.filter((s) => s.is_terminal).map((s) => s.id),
  )
  const listShown =
    view === 'list' && hideTerminal
      ? shown.filter((tk) => !terminalStateIds.has(tk.state_id))
      : shown
  const kanbanStates = showHidden
    ? wfStates
    : wfStates.filter((s) => !hiddenStateIds.has(s.id))

  return (
    <section className="card card--wide">
      <h1>{t('tasks.title')}</h1>

      <RecentTasks tasks={focused} />

      <form onSubmit={(e) => void onCreate(e)} className="quickadd">
        <input
          required
          placeholder={t('tasks.quickAdd')}
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          className="quickadd__title"
        />
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
        <button type="submit" disabled={busy || !title.trim()}>
          {busy ? t('tasks.saving') : t('tasks.create')}
        </button>
        <button
          type="button"
          className="btn--ghost"
          disabled={busy || !title.trim()}
          onClick={() =>
            void onCreate(
              // synthesize the FormEvent shape onCreate expects so
              // we don't duplicate the body. preventDefault is a no-op
              // on a synthetic click but the function calls it
              // unconditionally.
              { preventDefault: () => undefined } as FormEvent,
              true,
            )
          }
          title={t('tasks.createAndOpenHint')}
        >
          {t('tasks.createAndOpen')}
        </button>
      </form>
      <p className="hint">
        {t('tasks.cpManaged')}{' '}
        <Link to="/clients">{t('cp.nav')}</Link>
      </p>

      <div className="row">
        <input
          placeholder={t('tasks.search')}
          title={t('tasks.searchHint')}
          value={q}
          onChange={(e) => setQ(e.target.value)}
          style={{ flex: 1, minWidth: '12rem' }}
        />
        {searching && (
          <span
            className="hint"
            aria-live="polite"
            title={t('tasks.searchingHint')}
          >
            {t('tasks.searching')}
          </span>
        )}
        <div
          className="viewtabs"
          role="radiogroup"
          aria-label={t('tasks.identityFacet')}
        >
          {(['all', 'humans', 'bots'] as const).map((f) => (
            <button
              type="button"
              key={f}
              role="radio"
              aria-checked={identityFacet === f}
              className={
                'viewtabs__tab' +
                (identityFacet === f ? ' viewtabs__tab--active' : '')
              }
              onClick={() => setIdentityFacet(f)}
              title={t(`tasks.identity.${f}Hint`)}
            >
              {t(`tasks.identity.${f}`)}
            </button>
          ))}
        </div>
        <div
          className="filterbar__group"
          role="group"
          aria-label={t('tasks.scope.label')}
        >
          <div
            className="viewtabs"
            role="radiogroup"
            aria-label={t('tasks.scope.label')}
          >
            {SCOPES.map((s) => (
              <button
                key={s}
                type="button"
                role="radio"
                aria-checked={scope === s}
                className={
                  'viewtabs__tab' + (scope === s ? ' viewtabs__tab--active' : '')
                }
                onClick={() => setScope(s)}
              >
                {t(`tasks.scope.${s}`)}
              </button>
            ))}
          </div>
          <button
            type="button"
            role="switch"
            aria-checked={dateFocus}
            className={'toggle-pill' + (dateFocus ? ' toggle-pill--on' : '')}
            onClick={() => setDateFocus((v) => !v)}
            title={t('tasks.dateFocusHint')}
          >
            {t('tasks.dateFocus')}:{' '}
            {dateFocus ? t('common.on') : t('common.off')}
          </button>
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
              {t('tasks.showHidden')}:{' '}
              {showHidden ? t('common.on') : t('common.off')}
            </button>
          )}
          {view === 'list' && (
            <button
              type="button"
              role="switch"
              aria-checked={hideTerminal}
              className={
                'toggle-pill' + (hideTerminal ? ' toggle-pill--on' : '')
              }
              onClick={() => setHideTerminal((v) => !v)}
            >
              {t('tasks.hideTerminal')}:{' '}
              {hideTerminal ? t('common.on') : t('common.off')}
            </button>
          )}
        </div>
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
      {tags.length > 0 && (
        <div className="filterbar__tags">
          <span className="muted">{t('tasks.filterByTagLabel')}</span>
          <TagPickerGrid
            tags={tags}
            selected={filter ? [filter] : []}
            // Single-select: clicking the active chip clears the filter,
            // clicking another swaps it. Matches /notes' fTag behaviour
            // and the backend's single ``tag_id`` query param.
            onToggle={(id) => setFilter((cur) => (cur === id ? '' : id))}
            searchable={tags.length > 20}
          />
        </div>
      )}

      {err && <p className="err">{err}</p>}
      {bulkMsg && <p className="ok">{bulkMsg}</p>}

      {listShown.length > 0 && (
        <div
          className="filterbar__bulk"
          role="group"
          aria-label={t('tasks.bulkSection')}
        >
          <span className="filterbar__bulk-label">
            {t('tasks.bulkSection')}
          </span>
          {(() => {
            const allOn = sel.size > 0 && listShown.every((x) => sel.has(x.id))
            return (
              <button
                type="button"
                role="switch"
                aria-checked={allOn}
                className={
                  'toggle-pill' + (allOn ? ' toggle-pill--on' : '')
                }
                onClick={() =>
                  setSel(allOn ? new Set() : new Set(listShown.map((x) => x.id)))
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
                aria-label={t('tasks.bulkTagPick')}
              >
                <option value="">{t('tasks.bulkTagPick')}</option>
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
      ) : listShown.length === 0 ? (
        <p className="hint">{t('tasks.none')}</p>
      ) : (
        <ul className="list tasklist">
          {listShown.map((tk) => {
            const score = tk.importance * tk.urgency
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
                  {tk.assignee_kind ? (
                    <IdentityBadge
                      kind={tk.assignee_kind}
                      handle={tk.assignee_handle ?? null}
                    />
                  ) : tk.created_by_kind === 'ai_assistant' ||
                    tk.created_by_kind === 'mcp_token' ? (
                    // Created by an AI agent (identity-bound or bare
                    // MCP token); badge is always bot, label comes from
                    // ai_assistants.label OR agent_tokens.name.
                    <IdentityBadge
                      kind={tk.created_by_kind}
                      handle={tk.created_by_handle ?? null}
                      label={tk.created_by_label ?? null}
                      title={t('tasks.aiCreatedTitle', {
                        handle:
                          tk.created_by_label ?? tk.created_by_handle ?? '',
                      })}
                    />
                  ) : tk.executor_kind === 'llm_agent' ? (
                    // Unassigned but routed to the bot pool: keep the
                    // legacy aibadge marker (no handle to show).
                    <span className="aibadge" title={t('tasks.aiTitle')}>
                      {t('tasks.aiBadge')}
                    </span>
                  ) : null}
                  {tk.title}
                </Link>
                <span className="taskrow__tags">
                  {(tk.tags ?? []).map((g) => (
                    <TagChip key={g.id} name={g.name} color={g.color} kind={g.kind} />
                  ))}
                </span>
                <span className="taskrow__meta">
                  <span className="taskrow__sep" aria-hidden="true" />
                  {tk.start_at && tk.duration_minutes ? (
                    <span
                      className="muted"
                      title={t('tasks.eventTitle', {
                        when: new Date(tk.start_at).toLocaleString(),
                        minutes: tk.duration_minutes,
                      })}
                    >
                      🕒 {new Date(tk.start_at).toLocaleString([], {
                        month: 'short',
                        day: '2-digit',
                        hour: '2-digit',
                        minute: '2-digit',
                      })}
                      {' · '}
                      {tk.duration_minutes}m
                    </span>
                  ) : tk.due_date ? (
                    <span className="muted" title={t('tasks.due')}>
                      📅 {tk.due_date}
                    </span>
                  ) : null}
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
                  <TaskTimer taskId={tk.id} />
                </span>
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}
