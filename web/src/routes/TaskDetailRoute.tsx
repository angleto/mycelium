import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api, authFetch, errMessage, workspaceHeader } from '../api/client'
import { AnnotationsPanel } from '../components/AnnotationsPanel'
import { RefreshHint } from '../components/RefreshHint'
import { RichEditor, type AnnotationViewHandle } from '../components/RichEditor'
import { toAnchors, useAnnotations } from '../lib/useAnnotations'
import { useStaleWatch } from '../lib/useStaleWatch'
import { AssigneePicker } from '../components/AssigneePicker'
import { OwnerPicker } from '../components/OwnerPicker'
import { ParticipantsSection } from '../components/ParticipantsSection'
import { TagPicker } from '../components/TagPicker'
import { PriorityChip } from '../components/PriorityChip'
import { IdentityBadge } from '../components/IdentityBadge'
import { ScaleSelect } from '../components/ScaleSelect'
import { Attachments } from '../components/Attachments'
import { AgentRunPanel } from '../components/AgentRunPanel'
import { ChecklistPanel } from '../components/ChecklistPanel'
import { CoordinationPanel } from '../components/CoordinationPanel'
import { GardenSuggestionsPanel } from '../components/GardenSuggestionsPanel'
import { LinkedNotesPanel } from '../components/LinkedNotesPanel'
import { RevisionsPanel } from '../components/RevisionsPanel'
import { TaskTimer } from '../components/TaskTimer'
import { formatHours } from '../lib/estimate'
import { TASKS_LASTSEARCH_KEY } from '../lib/taskFilter'
import { useEditSession } from '../lib/useEditSession'
import { useUnsavedGuard } from '../lib/unsavedGuard'
import { useMediaQuery, MOBILE_QUERY } from '../lib/useMediaQuery'
import { pushRecent } from '../lib/recents'

import type { components } from '../shared'

type Task = components['schemas']['TaskOut']
type State = components['schemas']['StateOut']
type Tag = components['schemas']['TagOut']
type Project = components['schemas']['ProjectOut']
type Dep = components['schemas']['DependencyOut']
type Rel = components['schemas']['TaskRelationOut']

// Stable signature of an annotation set: id + version + status +
// soft-delete flag, sorted so member order never matters. Powers the
// focus-staleness probe — out-of-band comments / suggestions (e.g. added
// by an MCP tool) don't bump the task version, so the description
// annotations are checked separately.
function annoSig(
  rows: {
    id: string
    version: number
    status: string
    deleted_at?: string | null
  }[],
): string {
  return rows
    .map((r) => `${r.id}:${r.version}:${r.status}:${r.deleted_at ? 1 : 0}`)
    .sort()
    .join(',')
}

// Task detail with optimistic concurrency: edits send expected_version;
// a stale write yields 409 and we reload the canonical task.
export function TaskDetailRoute() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const { id = '' } = useParams()
  const [idCopied, setIdCopied] = useState(false)
  // Annotations on the task description (its work diary + review). One
  // shared fetch feeds both the inline editor decorations and the panel.
  const {
    rows: descAnnotations,
    reload: reloadDescAnnotations,
    error: descAnnoError,
  } = useAnnotations('task_description', id)
  const descAnchors = useMemo(() => toAnchors(descAnnotations), [descAnnotations])
  // Shared with the AnnotationsPanel below so its "go to text" button can
  // scroll the description editor to an annotation's anchored passage.
  const descViewRef = useRef<AnnotationViewHandle>(null)
  // "Add & Open" from TasksRoute hands us the freshly-created TaskOut
  // via router state. We hydrate from it on the very first render so
  // the user lands on the editable surface without a GET round-trip
  // (and without exposure to the create-commit / read race that
  // surfaced "Task not found" intermittently). Cleared after consumption
  // so a navigation away + back via deep link still triggers GET.
  const location = useLocation()
  // The "back to tasks" link returns to the filtered list the user came
  // from. ``q`` + tag filter live in the /tasks URL; the list route
  // mirrors its current search into sessionStorage per tab, so the link
  // restores it whatever entry point (row / kanban / recent) opened this
  // task. Read once on mount: stable for this view, and the browser Back
  // button independently restores the same URL from history.
  const [tasksBackSearch] = useState(() => {
    try {
      return sessionStorage.getItem(TASKS_LASTSEARCH_KEY) ?? ''
    } catch {
      return ''
    }
  })
  const seedTask = (location.state as { task?: Task } | null)?.task
  const seededTaskRef = useRef<Task | null>(
    seedTask && seedTask.id === id ? seedTask : null,
  )
  const [task, setTask] = useState<Task | null>(null)
  const [states, setStates] = useState<State[]>([])
  const [tags, setTags] = useState<Tag[]>([])
  // /projects, not the project tags: only ProjectOut carries
  // ``client_tag_id``, which couples the two structural selects.
  const [projects, setProjects] = useState<Project[]>([])
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  // Eisenhower axes are mandatory since migration 0102 (Low/Low default
  // applied at the backend). The initial render happens before ``task``
  // is loaded, hence the placeholder 4/4 here; ``apply()`` immediately
  // hydrates from the loaded TaskOut. The select is never nullable.
  const [importance, setImportance] = useState<number>(4)
  const [urgency, setUrgency] = useState<number>(4)
  const [estimate, setEstimate] = useState('')
  // Migration 0005: due_date is timestamptz, optional time-of-day.
  // The SPA splits the picker so the common "due by a calendar day"
  // workflow stays one input; setting a time is opt-in.
  // ``dueDate`` is "YYYY-MM-DD" (local), ``dueTime`` is "HH:MM" or ''.
  const [dueDate, setDueDate] = useState('')
  const [dueTime, setDueTime] = useState('')
  // Appointment editor (migration 0094, ADR-0008 addendum). The task
  // is a calendar appointment when both ``start_at`` and
  // ``duration_minutes`` are set. The block is opt-in (a toggle
  // expands it) so the majority "task with a due date" workflow stays
  // untouched.
  const [apptStartAt, setApptStartAt] = useState('')
  const [apptDuration, setApptDuration] = useState<number | ''>('')
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
  const [remCustom, setRemCustom] = useState(false)
  // Channels for the next reminder; empty = the user's default (all enabled).
  const [remChannels, setRemChannels] = useState<string[]>([])
  const [deps, setDeps] = useState<Dep[]>([])
  const [depOther, setDepOther] = useState('')
  const [depQuery, setDepQuery] = useState('')
  const [depOpen, setDepOpen] = useState(false)
  const [depRel, setDepRel] = useState<'depends' | 'blocks'>('depends')
  const [rels, setRels] = useState<Rel[]>([])
  const [relQuery, setRelQuery] = useState('')
  const [relOpen, setRelOpen] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  // Tag errors render inside the picker, not in the page-level banner
  // at the bottom of the body column: a rejected client/project change
  // (DomainError -> 400) has to be visible next to the control that
  // caused it, on the other side of the layout grid.
  const [tagErr, setTagErr] = useState<string | null>(null)
  const [workNotes, setWorkNotes] = useState<
    { id: string; title: string | null }[]
  >([])
  // Two tabs in the task body: markdown description and checklist
  // (no modality, both always available; the user picks what to use).
  // The last picked tab is remembered per task in localStorage so that
  // reopening a checklist-driven task lands directly on its checklist.
  const tabKey = `mycelium.task.${id}.activeTab`
  const [activeTab, setActiveTab] = useState<'description' | 'checklist'>(
    () => {
      try {
        const v = localStorage.getItem(tabKey)
        if (v === 'checklist' || v === 'description') return v
      } catch {
        /* private mode / quota: fall through to default */
      }
      return 'description'
    },
  )
  useEffect(() => {
    try {
      localStorage.setItem(tabKey, activeTab)
    } catch {
      /* ignore */
    }
  }, [tabKey, activeTab])
  // Unified "Connections" group: subtasks / dependencies / related /
  // notes share one tabbed surface. Active tab remembered per task
  // (mycelium.* localStorage namespace).
  const connKey = `mycelium.task.${id}.connTab`
  const [connTab, setConnTab] = useState<
    'subtasks' | 'deps' | 'related' | 'notes'
  >(() => {
    try {
      const v = localStorage.getItem(connKey)
      if (v === 'subtasks' || v === 'deps' || v === 'related' || v === 'notes')
        return v
    } catch {
      /* private mode / quota */
    }
    return 'subtasks'
  })
  useEffect(() => {
    try {
      localStorage.setItem(connKey, connTab)
    } catch {
      /* ignore */
    }
  }, [connKey, connTab])
  // Responsive: below the layout breakpoint the properties rail can't
  // sit beside the content, so it becomes a collapsible panel (closed
  // by default) above the body. Desktop renders it as a sticky rail.
  const isMobile = useMediaQuery(MOBILE_QUERY)
  const [propsOpen, setPropsOpen] = useState(false)
  // Feed the Cmd+K palette's "Recent" section. Write-only side effect:
  // it touches localStorage, never the render, so it cannot regress the
  // task view. Records once the task (and its title) have loaded.
  useEffect(() => {
    if (task) {
      pushRecent({
        kind: 'task',
        id: task.id,
        title: task.title,
        route: `/tasks/${task.id}`,
      })
    }
  }, [task])
  const [checklistCount, setChecklistCount] = useState<{
    done: number
    total: number
  }>({ done: 0, total: 0 })

  const apply = useCallback((tk: Task) => {
    setTask(tk)
    // Reset the autosave concurrency state to the freshly-loaded
    // canonical version: a reload after 409 must start the next
    // patch from this version, not from a stale ref.
    latestVersion.current = tk.version
    setTitle(tk.title)
    setDescription(tk.description ?? '')
    setImportance(tk.importance)
    setUrgency(tk.urgency)
    setEstimate(tk.estimate_effort_h ?? '')
    // Migration 0005: due_date is timestamptz. Split into a local
    // date + optional time; treat "end-of-day local" (= what the
    // backend stores when the user didn't pick a time) as "no time
    // specified", so the time input stays empty unless the user
    // explicitly set a different hour.
    if (tk.due_date) {
      const d = new Date(tk.due_date)
      const pad = (n: number) => String(n).padStart(2, '0')
      setDueDate(
        `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`,
      )
      const isEndOfDay =
        d.getHours() === 23 && d.getMinutes() === 59 && d.getSeconds() === 59
      setDueTime(
        isEndOfDay ? '' : `${pad(d.getHours())}:${pad(d.getMinutes())}`,
      )
    } else {
      setDueDate('')
      setDueTime('')
    }
    // Hydrate the appointment block. The datetime-local input wants
    // a naive local-tz string "YYYY-MM-DDTHH:MM"; the server returns
    // ISO UTC, convert.
    if (tk.start_at) {
      const d = new Date(tk.start_at)
      const pad = (n: number) => String(n).padStart(2, '0')
      setApptStartAt(
        `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`,
      )
    } else {
      setApptStartAt('')
    }
    setApptDuration(tk.duration_minutes ?? '')
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
    // Hydrate the task surface immediately from the navigation seed
    // (set by TasksRoute's "Add & Open"). The seed is the canonical
    // TaskOut the create endpoint returned, so we skip the GET on the
    // task itself — the GET sometimes raced the create commit and
    // surfaced "Task not found". Drop the ref so a later remount via
    // deep link refetches normally.
    const seed = seededTaskRef.current
    if (seed) {
      apply(seed)
      seededTaskRef.current = null
      // Drop the seed from history.state so back/forward doesn't
      // resurface a stale snapshot if the user edits then navigates
      // back. The URL stays put.
      navigate(location.pathname + location.search, {
        replace: true,
        state: null,
      })
    }
    void (async () => {
      const h = workspaceHeader()
      // When we have the seed we skip the task GET; otherwise it goes
      // in the Promise.all bundle as before. Modelled with a typed
      // sentinel so the tuple shape stays stable.
      const tkPromise = seed
        ? Promise.resolve(null)
        : api.GET('/tasks/{task_id}', {
            params: { header: h, path: { task_id: id } },
          })
      const [tk, st, tg, pj, all, dp, ws, rm, nt, rl] = await Promise.all([
        tkPromise,
        api.GET('/tasks/{task_id}/states', {
          params: { header: h, path: { task_id: id } },
        }),
        api.GET('/tags', { params: { header: h } }),
        // Archived included, as on the note detail: the payload is the
        // project -> client map behind TagPicker, not its option list.
        api.GET('/projects', {
          params: { header: h, query: { include_archived: true } },
        }),
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
      if (tk) {
        if (tk.data) apply(tk.data)
        else setErr(errMessage(tk.error))
      }
      if (st.data) setStates(st.data)
      if (tg.data) setTags(tg.data)
      if (pj.data) setProjects(pj.data)
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
    // ``location.*`` and ``navigate`` are only used to clear the
    // one-shot seed; they're stable identities and listing them would
    // re-run the whole mount fetch on every URL/state change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, apply])

  // Form submit (Enter in the title input) flushes the pending text
  // edit immediately rather than waiting for the 1s debounce. The
  // autosave path (flushText -> autosave) already carries optimistic
  // concurrency and edit-session coalescing, so there is no separate
  // save PATCH here anymore — the explicit "Save" button is gone.
  function onSave(e: FormEvent) {
    e.preventDefault()
    flushText()
  }

  async function onNewChild() {
    if (!task) return
    setErr(null)
    // Inherit the parent's structural pair BY NAME (the create door
    // checks each id's kind, so a child can't land on a mis-filed tag)
    // plus its generic facets. memory_channel tags are system
    // bookkeeping and are deliberately not carried over. Title is a
    // placeholder; the detail surface (autosave on title change) lets
    // the user rename inline.
    const parentTags = task.tags ?? []
    const inheritedTagIds = parentTags
      .filter((g) => g.kind === 'generic')
      .map((g) => g.id)
    const parentClient = parentTags.find((g) => g.kind === 'client')?.id
    const parentProject = parentTags.find((g) => g.kind === 'project')?.id
    const { data, error } = await api.POST('/tasks', {
      params: { header: workspaceHeader() },
      body: {
        title: t('tasks.newChildPlaceholder'),
        // Inherit the parent's Eisenhower axes: a child of a high-imp/
        // high-urg parent should not silently fall back to the Low/Low
        // backend default. ``priority`` is a calculated field and is
        // never an input.
        importance: task.importance,
        urgency: task.urgency,
        executor_kind: 'human',
        necessity: 'should',
        parent_task_id: task.id,
        tag_ids: inheritedTagIds,
        ...(parentClient ? { client_tag_id: parentClient } : {}),
        ...(parentProject ? { project_tag_id: parentProject } : {}),
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
    navigate(`/tasks${tasksBackSearch}`)
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
    navigate(`/tasks${tasksBackSearch}`)
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
  // we replace ``task`` with the canonical TaskOut from the PATCH
  // response so server-derived fields (priority above all) stay in
  // sync without a reload() — which would clobber an unsaved
  // title/description edit by overwriting the corresponding inputs.
  //
  // Concurrency: the SPA can fire several autosaves in quick succession
  // (importance + urgency + due date in <1s, or the debounced text save
  // racing a chip click). ``task.version`` is captured by React's
  // closure at render time, so two parallel autosaves would both send
  // the same ``expected_version`` and the second would 409 (kicking
  // off a reload storm; reproduces the bug logged on task fc321e26).
  //
  // Two ingredients fix this:
  // 1. ``latestVersion`` ref tracks the freshest server-confirmed
  //    version, independent of React render timing.
  // 2. ``autosaveChain`` ref serializes autosaves through a single
  //    Promise chain — each call waits for its predecessor to resolve
  //    so they always see the freshest version when they read it.
  // Recovery-history coalescing on the SPA: a per-task editing
  // session id rides every autosave PATCH as ``X-Edit-Session-Id``.
  // The server merges consecutive PATCHes that share it into one
  // open revision. When the user navigates away (unmount) or the
  // gap exceeds 30s, the session id is sealed and a fresh one will
  // be minted on the next edit. ``editSession.seal`` also fires the
  // explicit /edit-session/seal POST so the timeline shows the
  // window closed immediately, not 60s later via the worker.
  const editSession = useEditSession((sealedId) => {
    void api.POST('/tasks/{task_id}/edit-session/seal', {
      params: { header: workspaceHeader(), path: { task_id: id } },
      body: { edit_session_id: sealedId },
    })
  })

  // ``sink`` is where this patch reports: the page-level banner by
  // default, the tag picker's own slot for a structural re-tag, whose
  // rejection has to be readable next to the select that caused it.
  async function autosave(
    patch: Record<string, unknown>,
    sink: (m: string | null) => void = setErr,
  ) {
    if (!task) return
    const run = (autosaveChain.current ?? Promise.resolve()).then(
      async () => {
        sink(null)
        const v = latestVersion.current ?? task.version
        const sessionId = editSession.touch()
        const { data, error, response } = await api.PATCH(
          '/tasks/{task_id}',
          {
            params: {
              header: { ...workspaceHeader(), 'X-Edit-Session-Id': sessionId },
              path: { task_id: id },
            },
            body: { expected_version: v, ...patch },
          },
        )
        if (response.status === 409) {
          sink(t('tasks.conflict'))
          await reload()
          return
        }
        if (error || !data) {
          sink(errMessage(error))
          return
        }
        // Update both the ref (so the next queued autosave sees the
        // fresh version immediately) and the React state (so the rest
        // of the page renders the canonical TaskOut).
        latestVersion.current = data.version
        setTask(data)
      },
    )
    autosaveChain.current = run.catch(() => undefined)
    return run
  }

  // Title + description autosave: debounce 1s after the last change.
  // Skips the initial mount (no patch for unchanged values) and bails
  // if a sibling autosave already bumped the version while we were
  // typing — the optimistic-concurrency reload triggers, and the
  // debounced patch refires with the fresh version next tick.
  const lastSentText = useRef<{ title: string; description: string } | null>(
    null,
  )
  // Freshest server-confirmed version, decoupled from React render
  // closures. Seeded by ``apply()`` (initial load + reload paths) and
  // bumped by every successful autosave so the next queued autosave
  // never replays a stale ``expected_version``.
  const latestVersion = useRef<number | null>(null)
  // Single Promise chain that serializes autosaves: a click on
  // importance + a click on urgency 100ms apart resolve in order,
  // each seeing the version bump from the previous.
  const autosaveChain = useRef<Promise<void> | null>(null)
  // Flush the pending title/description edit *now*, bypassing the 1s
  // debounce. Centralises the diff so every flush path (debounce, blur,
  // Enter, tab-hide, navigation/unmount, page unload) is identical.
  // ``keepalive`` routes through a bare fetch so an in-flight PATCH
  // survives the page being torn down (beforeunload / tab hidden);
  // otherwise it goes through the serialized autosave chain.
  function flushText(keepalive = false) {
    const prev = lastSentText.current
    if (!prev || !task) return
    const curDescr = description ?? ''
    const patch: Record<string, unknown> = {}
    if (title !== prev.title) patch.title = title
    if (curDescr !== prev.description) patch.description = curDescr || null
    if (Object.keys(patch).length === 0) return
    lastSentText.current = { title, description: curDescr }
    if (keepalive) {
      const v = latestVersion.current ?? task.version
      const sessionId = editSession.current() ?? editSession.touch()
      void authFetch(`/tasks/${id}`, {
        method: 'PATCH',
        keepalive: true,
        headers: {
          'content-type': 'application/json',
          'X-Edit-Session-Id': sessionId,
        },
        body: JSON.stringify({ expected_version: v, ...patch }),
      })
    } else {
      void autosave(patch)
    }
  }
  // Keep a ref to the latest ``flushText`` closure so the mount-once
  // listeners below always flush the current draft, not a stale one.
  const flushRef = useRef(flushText)
  useEffect(() => {
    flushRef.current = flushText
  })

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
    const handle = window.setTimeout(() => flushText(), 1000)
    return () => window.clearTimeout(handle)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [title, description, task?.version])

  // Data-loss guard. The debounced flush above is cancelled on unmount
  // (its cleanup clears the timer), so an edit made <1s before leaving
  // would otherwise be dropped — the failure the manual Save button was
  // standing in for. Flush on tab-hide and page unload (keepalive, so
  // the PATCH outlives a closing page) and on unmount = SPA navigation.
  useEffect(() => {
    const onVis = () => {
      if (document.visibilityState === 'hidden') flushRef.current(true)
    }
    const onUnload = () => flushRef.current(true)
    document.addEventListener('visibilitychange', onVis)
    window.addEventListener('beforeunload', onUnload)
    return () => {
      document.removeEventListener('visibilitychange', onVis)
      window.removeEventListener('beforeunload', onUnload)
      flushRef.current()
    }
  }, [])

  // Out-of-band change detection: an MCP tool / CLI / another device can
  // write to this task while the view shows a stale snapshot (the server
  // bumps ``version`` on every write but never pushes). On tab focus we
  // re-probe: the task ``version`` covers title / description / fields,
  // and the description annotation set covers comments & suggestions
  // added out of band (those don't bump the task version). A newer
  // server state raises a non-destructive "changed elsewhere" banner; we
  // never overwrite an in-progress edit.
  const knownAnnoSig = useRef('')
  useEffect(() => {
    knownAnnoSig.current = annoSig(descAnnotations)
  }, [descAnnotations])

  const taskStaleProbe = useCallback(async (): Promise<boolean> => {
    const { data, error } = await api.GET('/tasks/{task_id}', {
      params: { header: workspaceHeader(), path: { task_id: id } },
    })
    if (error || !data) return false
    if (latestVersion.current !== null && data.version !== latestVersion.current)
      return true
    const qs = new URLSearchParams({
      doc_kind: 'task_description',
      doc_id: id,
      include_resolved: 'true',
    })
    const ares = await authFetch(`/annotations?${qs.toString()}`)
    if (!ares.ok) return false
    const rows = (await ares.json()) as {
      id: string
      version: number
      status: string
      deleted_at?: string | null
    }[]
    return annoSig(rows) !== knownAnnoSig.current
  }, [id])

  const { stale, reset: resetStale } = useStaleWatch({
    enabled: !!task,
    resetKey: id,
    probe: taskStaleProbe,
  })

  // Reload from server truth, discarding local title/description drafts
  // (the user accepted that when clicking Reload on the banner):
  // ``reload`` -> ``apply`` rehydrates every field, and the annotations
  // are refetched alongside.
  async function reloadStale() {
    await reload()
    await reloadDescAnnotations()
    resetStale()
  }

  // Send only the changed axis: if the other axis was unset (NULL in
  // the DB) we must not silently push the local default. The backend
  // re-derives ``priority`` whenever both axes end up non-NULL after
  // the patch (and leaves it untouched otherwise).
  function onImp(n: number) {
    setImportance(n)
    void autosave({ importance: n })
  }
  function onUrg(n: number) {
    setUrgency(n)
    void autosave({ urgency: n })
  }
  // What we send to /tasks/{id} as ``due_date``: empty date clears the
  // deadline; with NO time we send a bare ``YYYY-MM-DD`` (date-only
  // intent) and the backend anchors it to end-of-day in the user's
  // configured timezone (the single source of truth, so SPA/MCP/API
  // agree); with a time we send the explicit instant as a UTC ISO.
  function buildDueIso(date: string, time: string): string | null {
    if (!date) return null
    if (!time) return date
    // datetime-local interprets "YYYY-MM-DDTHH:MM(:SS)?" as local;
    // toISOString then yields the matching UTC instant.
    return new Date(`${date}T${time}`).toISOString()
  }
  function onDueDate(v: string) {
    setDueDate(v)
    void autosave({ due_date: buildDueIso(v, dueTime) })
  }
  function onDueTime(v: string) {
    setDueTime(v)
    // Setting only a time without a date is meaningless; we autosave
    // only when the date is present (the date input is the anchor).
    if (dueDate) void autosave({ due_date: buildDueIso(dueDate, v) })
  }
  // Appointment promotion / edit (migration 0094). Save the pair
  // atomically: the backend CHECK constraint rejects half-set inputs,
  // so we ONLY autosave once both fields have a value. A partial
  // input (user just typed the start, hasn't filled duration yet) is
  // a no-op — earlier we cleared both here, which reset the input the
  // user had just typed (reproduced on task a3d1f5f4 item 2).
  //
  // Demoting back to a plain task is a deliberate action: the user
  // clicks "clear appointment" → ``clearAppointment()`` below sends
  // the explicit ``{start_at: null, duration_minutes: null}`` pair.
  async function saveAppointment(startLocal: string, minutes: number | '') {
    if (!startLocal || !minutes) return
    const iso = new Date(startLocal).toISOString()
    await autosave({ start_at: iso, duration_minutes: Number(minutes) })
  }
  function onApptStart(v: string) {
    setApptStartAt(v)
    void saveAppointment(v, apptDuration)
  }
  function onApptDuration(v: string) {
    const n = v ? Number(v) : ''
    setApptDuration(n)
    void saveAppointment(apptStartAt, n)
  }
  function clearAppointment() {
    setApptStartAt('')
    setApptDuration('')
    void autosave({ start_at: null, duration_minutes: null })
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
    // authFetch, not the typed client: ReminderIn.channels is not in the
    // committed OpenAPI schema yet, and regenerating it would pull in
    // unrelated in-flight changes.
    const res = await authFetch(`/tasks/${id}/reminders`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        offset_minutes: Number(remOff),
        channels: remChannels.length ? remChannels : null,
      }),
    })
    if (!res.ok) {
      setErr(t('error.generic'))
      return
    }
    setRemChannels([])
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
    navigate(`/notes/${data.id}`)
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

  // Free-form facets only: the structural pair moves through PATCH
  // (see the TagPicker below), and detaching either of the two is
  // refused by the API anyway (TAG_STRUCTURAL_REQUIRED).
  async function addTag(tagId: string) {
    setTagErr(null)
    const { error } = await api.POST('/tasks/{task_id}/tags', {
      params: { header: workspaceHeader(), path: { task_id: id } },
      body: { tag_id: tagId },
    })
    if (error) {
      setTagErr(errMessage(error))
      return
    }
    await reload()
  }

  async function removeTag(tagId: string) {
    setTagErr(null)
    const { error } = await api.DELETE('/tasks/{task_id}/tags/{tag_id}', {
      params: {
        header: workspaceHeader(),
        path: { task_id: id, tag_id: tagId },
      },
    })
    if (error) {
      setTagErr(errMessage(error))
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
    navigate(`/notes/${data.id}`)
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

  // Autosave covers every field. A "save now" button is intentionally
  // gone — the debounced effect above flushes title/description ~1s
  // after the user stops typing, and onSave is kept only as the form's
  // onSubmit fallback (Enter in the title input still saves
  // immediately instead of waiting for the debounce).
  //
  // Computed ABOVE the early returns because useUnsavedGuard is a hook:
  // a debounce window still pending is exactly the moment an automatic
  // reload onto a new build would eat the user's typing.
  const dirty =
    !!task && (title !== task.title || description !== (task.description ?? ''))
  useUnsavedGuard(dirty)

  if (err && !task) return <p className="err">{err}</p>
  if (!task) return <p>{t('tasks.loading')}</p>

  async function copyId() {
    try {
      await navigator.clipboard.writeText(id)
      setIdCopied(true)
      window.setTimeout(() => setIdCopied(false), 1500)
    } catch {
      setIdCopied(false)
    }
  }

  return (
    <section className="card card--wide taskdetail">
      {stale && (
        <RefreshHint
          dirty={dirty}
          onReload={() => void reloadStale()}
          onDismiss={resetStale}
        />
      )}
      <header className="taskdetail__header">
        <p className="hint taskdetail__back">
          <Link to={`/tasks${tasksBackSearch}`}>{t('tasks.back')}</Link>
        </p>
        {/* The two most-used controls (advance the state, start/stop the
            clock) plus the autosave status live in the header so they
            stay visible on every breakpoint — never behind the mobile
            properties accordion. The structural actions (new child,
            archive, delete) sit here too, no longer buried at the bottom
            of the form. The "State" caption is dropped; the options name
            the control and the aria-label carries it for screen readers. */}
        <div className="taskdetail__headeractions">
          <span className="taskdetail__savestate hint" aria-live="polite">
            {dirty ? t('tasks.unsaved') : t('tasks.saved')}
          </span>
          <button
            type="button"
            className="chip chip--copy"
            title={idCopied ? t('tasks.idCopied') : id}
            aria-label={t('tasks.copyId')}
            onClick={() => void copyId()}
          >
            {idCopied ? t('tasks.idCopied') : `ID ${id.slice(0, 8)}…`}
          </button>
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
          <span className="taskdetail__headersep" aria-hidden="true" />
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
      </header>
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
        <p className="taskdetail__badges">
          <IdentityBadge
            kind={task.assignee_kind}
            handle={task.assignee_handle ?? null}
          />
        </p>
      ) : task.created_by_kind === 'ai_assistant' ||
        task.created_by_kind === 'mcp_token' ? (
        <p className="taskdetail__badges">
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
        <p className="taskdetail__badges">
          <span className="aibadge" title={t('tasks.aiTitle')}>
            {t('tasks.aiBadge')}
          </span>
        </p>
      ) : null}
      {/* Title spans full width above the two-pane grid (issue-tracker
          layout). Its own <form> keeps Enter-to-flush; onBlur flushes
          too, so the title is never lost on a quick exit. */}
      <form className="taskdetail__titleform" onSubmit={(e) => void onSave(e)}>
        <label className="taskdetail__titlelabel">
          {t('tasks.newTitle')}
          <input
            required
            className="taskdetail__title"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            onBlur={() => flushText()}
          />
        </label>
      </form>
      <div className="taskdetail__grid">
        <main className="taskdetail__main">
        <div className="field taskdetail__body">
          {/* Two tabs side by side: markdown body and structured checklist.
              Both fields live on every task; the user picks what to use.
              Voice / agent automations target the checklist via the
              dedicated /tasks/{id}/checklist endpoints. */}
          <div
            className="tabs"
            role="tablist"
            aria-label={t('tasks.description')}
          >
            <button
              type="button"
              role="tab"
              aria-selected={activeTab === 'description'}
              className={`tabs__tab${activeTab === 'description' ? ' is-active' : ''}`}
              onClick={() => setActiveTab('description')}
            >
              {t('tasks.descriptionTab')}
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={activeTab === 'checklist'}
              className={`tabs__tab${activeTab === 'checklist' ? ' is-active' : ''}`}
              onClick={() => setActiveTab('checklist')}
            >
              {t('tasks.checklistTab')}{' '}
              <span className="muted">
                {t('tasks.checklistCount', {
                  done: checklistCount.done,
                  total: checklistCount.total,
                })}
              </span>
            </button>
          </div>
          <div role="tabpanel" hidden={activeTab !== 'description'}>
            <RichEditor
              value={description}
              onChange={setDescription}
              imageUploadParent={{ kind: 'task', id: task.id }}
              filename={task.title}
              annotations={descAnchors}
              inlineAnnotations={{
                docKind: 'task_description',
                docId: id,
                rows: descAnnotations,
                reload: reloadDescAnnotations,
                onDocMutated: reload,
              }}
              viewRef={descViewRef}
            />
            <AnnotationsPanel
              docKind="task_description"
              docId={id}
              rows={descAnnotations}
              reload={reloadDescAnnotations}
              loadError={descAnnoError}
              onDocMutated={reload}
              imageUploadParent={{ kind: 'task', id: task.id }}
              title={t('annotations.diaryTitle', {
                defaultValue: 'Work diary, comments & suggestions',
              })}
              onJumpToAnchor={(a) => descViewRef.current?.scrollToAnnotation(a) ?? false}
            />
          </div>
          <div role="tabpanel" hidden={activeTab !== 'checklist'}>
            <ChecklistPanel
              owner={{ kind: 'task', id: task.id }}
              initial={task.checklist ?? []}
              onCountChange={(done, total) =>
                setChecklistCount({ done, total })
              }
              disabled={task.deleted_at != null || task.is_archived}
            />
          </div>
        </div>
        {err && <p className="err">{err}</p>}
        </main>
        <aside
          className={
            'taskdetail__aside' +
            (isMobile && !propsOpen ? ' taskdetail__aside--collapsed' : '')
          }
        >
          {isMobile ? (
            <button
              type="button"
              className="taskdetail__propstoggle"
              aria-expanded={propsOpen}
              onClick={() => setPropsOpen((v) => !v)}
            >
              {t('tasks.properties')}
              <span aria-hidden="true">{propsOpen ? ' ▾' : ' ▸'}</span>
            </button>
          ) : (
            <h2 className="taskdetail__asideh">{t('tasks.properties')}</h2>
          )}
          {(!isMobile || propsOpen) && (
            <div className="taskdetail__asidebody">
        <div className="row">
          <label>
            {t('tasks.importance')}
            <ScaleSelect
              value={importance}
              onChange={onImp}
              labelsKey="tasks.impLabels"
            />
          </label>
          <label>
            {t('tasks.urgency')}
            <ScaleSelect
              value={urgency}
              onChange={onUrg}
              labelsKey="tasks.urgLabels"
            />
          </label>
          {/* priority comes from the server (importance x urgency); the
              SPA never derives it in JS. Both axes are mandatory since
              migration 0102, so the score is always surfaced in the
              tooltip. */}
          <PriorityChip
            priority={task.priority}
            score={task.importance * task.urgency}
          />
        </div>
        <div className="row">
          <label>
            {t('tasks.due')}
            <input
              type="date"
              value={dueDate}
              onChange={(e) => onDueDate(e.target.value)}
            />
          </label>
          <label>
            {t('tasks.dueTime')}
            <input
              type="time"
              value={dueTime}
              onChange={(e) => onDueTime(e.target.value)}
              disabled={!dueDate}
            />
          </label>
        </div>
        <fieldset className="taskdetail__appointment">
          <legend>{t('tasks.appointment')}</legend>
          <p className="hint">{t('tasks.appointmentHint')}</p>
          <div className="row">
            <label>
              {t('tasks.appointmentStart')}
              <input
                type="datetime-local"
                value={apptStartAt}
                onChange={(e) => onApptStart(e.target.value)}
              />
            </label>
            <label>
              {t('tasks.appointmentDuration')}
              <input
                type="number"
                min={1}
                step={5}
                value={apptDuration}
                onChange={(e) => onApptDuration(e.target.value)}
                placeholder="min"
              />
            </label>
            {(apptStartAt || apptDuration) && (
              <button
                type="button"
                className="ghost"
                onClick={() => clearAppointment()}
                title={t('tasks.appointmentClear')}
              >
                {t('tasks.appointmentClear')}
              </button>
            )}
          </div>
        </fieldset>
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
            {reminders.map((r) => {
              const ch = (r as { channels?: string[] | null }).channels
              return (
                <span key={r.id} className="chip">
                  {fmtOffset(r.offset_minutes)}
                  {ch && ch.length ? ` · ${ch.join('/')}` : ''}
                  <button
                    type="button"
                    className="btn--ghost btn--sm"
                    onClick={() => void removeReminder(r.id)}
                  >
                    ✕
                  </button>
                </span>
              )
            })}
          </div>
          <div className="row">
            <select
              value={remCustom ? 'custom' : remOff}
              onChange={(e) => {
                const v = e.target.value
                if (v === 'custom') {
                  setRemCustom(true)
                } else {
                  setRemCustom(false)
                  setRemOff(v)
                }
              }}
            >
              <option value="0">{t('tasks.remAtDue')}</option>
              <option value="5">{t('tasks.remBefore', { v: '5m' })}</option>
              <option value="10">{t('tasks.remBefore', { v: '10m' })}</option>
              <option value="30">{t('tasks.remBefore', { v: '30m' })}</option>
              <option value="60">{t('tasks.remBefore', { v: '1h' })}</option>
              <option value="240">{t('tasks.remBefore', { v: '4h' })}</option>
              <option value="1440">{t('tasks.remBefore', { v: '1d' })}</option>
              <option value="2880">{t('tasks.remBefore', { v: '2d' })}</option>
              <option value="10080">{t('tasks.remBefore', { v: '1w' })}</option>
              <option value="custom">{t('tasks.remCustom')}</option>
            </select>
            {remCustom && (
              <input
                type="text"
                inputMode="numeric"
                pattern="[0-9]*"
                placeholder={t('tasks.remCustomPh')}
                value={remOff}
                onChange={(e) => {
                  // Reminder offsets are integer minutes; strip
                  // anything that is not a digit so users can't end
                  // up with "0,50" (Italian decimal) or similar.
                  const digits = e.target.value.replace(/\D+/g, '')
                  setRemOff(digits)
                }}
              />
            )}
            <span className="hint">{t('tasks.remChannels')}</span>
            {['email', 'telegram', 'webpush'].map((c) => (
              <label key={c} className="chip">
                <input
                  type="checkbox"
                  checked={remChannels.includes(c)}
                  onChange={(e) =>
                    setRemChannels((xs) =>
                      e.target.checked ? [...xs, c] : xs.filter((x) => x !== c),
                    )
                  }
                />{' '}
                {c}
              </label>
            ))}
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
              <h2 className="taskdetail__asideh">{t('tasks.tagsTitle')}</h2>
              <TagPicker
                selected={task?.tags ?? []}
                all={tags}
                error={tagErr}
                structural={{
                  mode: 'task',
                  projects,
                  // The named pair on TaskPatchIn, not attach/detach on
                  // /tasks/{id}/tags: "this task is now on that
                  // project" is ONE intent, it rides the same
                  // expected_version envelope as every other field, and
                  // the response is the canonical TaskOut. A task never
                  // drops its pair (TAG_STRUCTURAL_REQUIRED), so null
                  // cannot reach here from mode="task".
                  onSetClient: (cid) => {
                    if (cid) void autosave({ client_tag_id: cid }, setTagErr)
                  },
                  onSetProject: (pid) => {
                    if (pid) void autosave({ project_tag_id: pid }, setTagErr)
                  },
                }}
                onAdd={(tid) => void addTag(tid)}
                onRemove={(tid) => void removeTag(tid)}
              />
            </div>
          )}
        </aside>
      </div>

      <section className="taskdetail__connections">
        <h2 className="taskdetail__connh">{t('tasks.connections')}</h2>
        <div
          className="tabs"
          role="tablist"
          aria-label={t('tasks.connections')}
        >
          <button
            type="button"
            role="tab"
            aria-selected={connTab === 'subtasks'}
            className={`tabs__tab${connTab === 'subtasks' ? ' is-active' : ''}`}
            onClick={() => setConnTab('subtasks')}
          >
            {t('tasks.subtasks')}
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={connTab === 'deps'}
            className={`tabs__tab${connTab === 'deps' ? ' is-active' : ''}`}
            onClick={() => setConnTab('deps')}
          >
            {t('tasks.deps')}
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={connTab === 'related'}
            className={`tabs__tab${connTab === 'related' ? ' is-active' : ''}`}
            onClick={() => setConnTab('related')}
          >
            {t('tasks.related')}
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={connTab === 'notes'}
            className={`tabs__tab${connTab === 'notes' ? ' is-active' : ''}`}
            onClick={() => setConnTab('notes')}
          >
            {t('tasks.connNotes')}
          </button>
        </div>
        <div role="tabpanel" hidden={connTab !== 'deps'}>
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
        </div>
        <div role="tabpanel" hidden={connTab !== 'subtasks'}>
          {task.parent_task_id == null && subtasks.length === 0 && (
            <p className="hint">{t('tasks.subtasksNone')}</p>
          )}
          {(task.parent_task_id || subtasks.length > 0) && (
            <>
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
        </div>
        <div role="tabpanel" hidden={connTab !== 'related'}>
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
        </div>
        <div role="tabpanel" hidden={connTab !== 'notes'}>
          <LinkedNotesPanel taskId={id} />
          <div className="row">
            <button
              type="button"
              className="btn--sm"
              onClick={() => void openWorkNote()}
            >
              {t('tasks.workNote')}
            </button>
            <button
              type="button"
              className="btn--sm"
              onClick={() => void newWorkNote()}
            >
              {t('tasks.newWorkNote')}
            </button>
          </div>
          {workNotes.length === 0 ? (
            <p className="hint">{t('tasks.noWorkNotes')}</p>
          ) : (
            <ul className="list">
              {workNotes.map((n) => (
                <li key={n.id}>
                  <Link to={`/notes/${n.id}`}>
                    {n.title || t('notes.untitled')}
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </div>
      </section>

      <h2>{t('coord.title')}</h2>
      <CoordinationPanel
        taskId={id}
        offered={task.offered ?? false}
        titleOf={titleOf}
        onChanged={() => void reload()}
      />

      <GardenSuggestionsPanel nodeId={id} nodeKind="task" onApplied={() => void reload()} />

      {task.duration_minutes != null && task.start_at != null && (
        <ParticipantsSection
          taskId={id}
          assigneeIdentityId={task.assignee_id ?? null}
        />
      )}

      <h2>{t('attach.title')}</h2>
      <Attachments taskId={id} />

      {task.executor_kind === 'llm_agent' && (
        <>
          <h2>{t('agentrun.title')}</h2>
          <AgentRunPanel taskId={id} />
        </>
      )}

      <RevisionsPanel
        kind="task"
        id={id}
        version={task.version}
        current={task as unknown as Record<string, unknown>}
        onRestored={() => void reload()}
      />
    </section>
  )
}
