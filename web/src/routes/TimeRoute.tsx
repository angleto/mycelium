import {
  Fragment,
  useCallback,
  useEffect,
  useState,
  type FormEvent,
} from 'react'
import { useTranslation } from 'react-i18next'
import { api, authFetch, errMessage, workspaceHeader } from '../api/client'
import { useSession } from '../auth/useSession'
import {
  fmtDateTime,
  fmtClock as fmtClockTz,
  toLocalInput,
  fromLocalInput,
} from '../lib/tz'
import { useLinkedClientProject } from '../lib/linkedClientProject'
import { useWorkflowStates } from '../lib/useWorkflowStates'
import { TaskPickList } from '../components/TaskPickList'
import type { components } from '../api/schema'

type Task = components['schemas']['TaskOut']
type Entry = components['schemas']['TimeEntryOut']
type TaskRep = components['schemas']['TaskTimeReportOut']
type Row = components['schemas']['ReportRowOut']
type Group = components['schemas']['ReportGroup']
type Tag = components['schemas']['TagOut']
type Project = components['schemas']['ProjectOut']
type Scope = 'all' | 'mine' | 'ai'
type BillableF = 'all' | 'yes' | 'no'

const GROUPS: Group[] = ['project', 'client', 'generic', 'user', 'task']
const ENT_PAGE = 50

// Endpoints of the current local month as YYYY-MM-DD. Local rather
// than UTC so the user always sees "1st of this month" matching their
// calendar; the backend pairs these with 00:00:00 / 23:59:59 to form
// the inclusive query range.
function pad2(n: number): string {
  return String(n).padStart(2, '0')
}
// Period selector helpers (#65). Each returns { from, to, label,
// prevAnchor, nextAnchor } so the SPA can render navigation + a
// previous-period diff fetch. The anchor is a Date stamped at noon
// local to avoid DST edge cases when stepping by 1 month / 1 week.
type Period = 'day' | 'week' | 'month' | 'custom'

function ymdLocal(d: Date): string {
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`
}

function periodRange(period: Period, anchor: Date): {
  from: string
  to: string
  label: string
  prevAnchor: Date
  nextAnchor: Date
  prevFrom: string
  prevTo: string
} {
  const a = new Date(anchor.getFullYear(), anchor.getMonth(), anchor.getDate(), 12)
  if (period === 'day') {
    const from = ymdLocal(a)
    const to = ymdLocal(a)
    const label = a.toLocaleDateString(undefined, {
      weekday: 'short',
      day: '2-digit',
      month: 'short',
      year: 'numeric',
    })
    const prev = new Date(a)
    prev.setDate(prev.getDate() - 1)
    const next = new Date(a)
    next.setDate(next.getDate() + 1)
    return {
      from,
      to,
      label,
      prevAnchor: prev,
      nextAnchor: next,
      prevFrom: ymdLocal(prev),
      prevTo: ymdLocal(prev),
    }
  }
  if (period === 'week') {
    // ISO-ish: week starts on Monday. getDay() = 0 (Sun) .. 6 (Sat).
    const day = a.getDay() === 0 ? 7 : a.getDay()
    const start = new Date(a)
    start.setDate(a.getDate() - (day - 1))
    const end = new Date(start)
    end.setDate(start.getDate() + 6)
    const label = `${start.toLocaleDateString(undefined, { day: '2-digit', month: 'short' })} – ${end.toLocaleDateString(undefined, { day: '2-digit', month: 'short', year: 'numeric' })}`
    const prevStart = new Date(start)
    prevStart.setDate(prevStart.getDate() - 7)
    const prevEnd = new Date(prevStart)
    prevEnd.setDate(prevEnd.getDate() + 6)
    const nextAnchor = new Date(start)
    nextAnchor.setDate(start.getDate() + 7)
    return {
      from: ymdLocal(start),
      to: ymdLocal(end),
      label,
      prevAnchor: prevStart,
      nextAnchor,
      prevFrom: ymdLocal(prevStart),
      prevTo: ymdLocal(prevEnd),
    }
  }
  // month
  const start = new Date(a.getFullYear(), a.getMonth(), 1, 12)
  const end = new Date(a.getFullYear(), a.getMonth() + 1, 0, 12)
  const label = start.toLocaleDateString(undefined, {
    month: 'long',
    year: 'numeric',
  })
  const prevStart = new Date(a.getFullYear(), a.getMonth() - 1, 1, 12)
  const prevEnd = new Date(a.getFullYear(), a.getMonth(), 0, 12)
  const nextAnchor = new Date(a.getFullYear(), a.getMonth() + 1, 1, 12)
  return {
    from: ymdLocal(start),
    to: ymdLocal(end),
    label,
    prevAnchor: prevStart,
    nextAnchor,
    prevFrom: ymdLocal(prevStart),
    prevTo: ymdLocal(prevEnd),
  }
}

function hhmmss(sec: number): string {
  const s = Math.max(0, Math.floor(sec))
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  return `${h}h ${String(m).padStart(2, '0')}m ${String(s % 60).padStart(2, '0')}s`
}

const PIE = [
  '#6d28d9',
  '#0ea5e9',
  '#16a34a',
  '#d97706',
  '#dc2626',
  '#0891b2',
  '#9333ea',
  '#65a30d',
  '#e11d48',
]

// Dependency-free SVG donut (stroke-dasharray technique) of the
// time distribution per slice, with a legend.
function Donut({ data }: { data: { label: string; secs: number }[] }) {
  const total = data.reduce((s, d) => s + d.secs, 0) || 1
  const r = 42
  const C = 2 * Math.PI * r
  // Functional prefix-sums: each slice starts where the prior ones
  // ended. No mutable accumulator (React Compiler forbids reassigning
  // an outer variable across the map closure during render).
  const fracs = data.map((d) => d.secs / total)
  const offsets = fracs.map((_, i) =>
    fracs.slice(0, i).reduce((s, x) => s + x, 0),
  )
  return (
    <div className="timepie">
      <svg viewBox="0 0 120 120" width="150" height="150" role="img">
        <g transform="rotate(-90 60 60)">
          {data.map((_, i) => (
            <circle
              key={i}
              cx="60"
              cy="60"
              r={r}
              fill="none"
              stroke={PIE[i % PIE.length]}
              strokeWidth="16"
              strokeDasharray={`${fracs[i] * C} ${C}`}
              strokeDashoffset={-offsets[i] * C}
            />
          ))}
        </g>
      </svg>
      <ul className="pielegend">
        {data.map((d, i) => (
          <li key={i}>
            <span
              className="pieswatch"
              style={{ background: PIE[i % PIE.length] }}
            />
            <span className="grow">{d.label}</span>
            <span className="muted">
              {hhmmss(d.secs)} · {Math.round((d.secs / total) * 100)}%
            </span>
          </li>
        ))}
      </ul>
    </div>
  )
}


export function TimeRoute() {
  const { t } = useTranslation()
  const session = useSession()
  const activeId = session?.workspaceId
  const [tasks, setTasks] = useState<Task[]>([])
  const [running, setRunning] = useState<Entry[]>([])
  const [entries, setEntries] = useState<Entry[]>([])
  const [entOffset, setEntOffset] = useState(0)
  const [entMore, setEntMore] = useState(false)
  const [entLoading, setEntLoading] = useState(false)
  const [report, setReport] = useState<Row[]>([])
  const [group, setGroup] = useState<Group>('project')
  // The pie chart can be grouped independently from the report table
  // — three buckets only: task / project / client. Persisted in
  // localStorage; default = project.
  type PieGroup = 'task' | 'project' | 'client'
  const PIE_KEY = 'flow.time.pieGroup'
  const [pieGroup, setPieGroup] = useState<PieGroup>(() => {
    try {
      const saved = localStorage.getItem(PIE_KEY)
      if (saved === 'task' || saved === 'project' || saved === 'client') return saved
    } catch {
      /* fall through */
    }
    return 'project'
  })
  useEffect(() => {
    try {
      localStorage.setItem(PIE_KEY, pieGroup)
    } catch {
      /* ignore */
    }
  }, [pieGroup])
  const [pieReport, setPieReport] = useState<Row[]>([])
  const [scope, setScope] = useState<Scope>('all')
  // Default range = current month (1st .. last day, inclusive). The
  // backend treats start_from/start_to as ``[from 00:00:00, to 23:59:59]``,
  // so the bounds here are the local YYYY-MM-DD endpoints of the month.
  // #65: period selector. Default = month (current). The anchor is
  // the Date inside the period; navigation steps it. ``period='custom'``
  // is set when the user types into the from/to inputs; the selector
  // chips switch back to month/week/day by re-anchoring to today.
  const [period, setPeriod] = useState<Period>('month')
  const [periodAnchor, setPeriodAnchor] = useState<Date>(() => new Date())
  const periodInfo =
    period === 'custom' ? null : periodRange(period, periodAnchor)
  const [from, setFrom] = useState(() => periodRange('month', new Date()).from)
  const [to, setTo] = useState(() => periodRange('month', new Date()).to)
  // Total seconds + amount for the previous-period diff. Loaded by a
  // dedicated useEffect keyed on the previous-range (skipped when
  // period='custom' since "previous" is undefined for arbitrary ranges).
  const [prevTotal, setPrevTotal] = useState<{
    seconds: number
    amount: string
  } | null>(null)
  // Sync from/to when the period selector or anchor moves. When the
  // user is in custom mode the picker stays static; the from/to
  // inputs remain user-controlled.
  useEffect(() => {
    if (!periodInfo) return
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setFrom(periodInfo.from)
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setTo(periodInfo.to)
    // periodInfo is derived from period + periodAnchor; ESLint can't
    // see that.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [period, periodAnchor])

  const [billableF, setBillableF] = useState<BillableF>('all')
  const [clients, setClients] = useState<Tag[]>([])
  const [projects, setProjects] = useState<Tag[]>([])
  const [allTags, setAllTags] = useState<Tag[]>([])
  const wfStates = useWorkflowStates()
  const [projectProfiles, setProjectProfiles] = useState<Project[]>([])
  const {
    clientId,
    projectId,
    onPickClient,
    onPickProject,
    filterProjectsByClient,
  } = useLinkedClientProject(projectProfiles)
  const projectsForClient = filterProjectsByClient(projects)
  const [pick, setPick] = useState('')
  const [now, setNow] = useState<number>(() => Date.now())
  // Force-refresh counter for the report / pie chart effects. Bumped
  // by saveEntry / deleteEntry / startTask / stopTimer so the
  // dependent useEffects re-fetch the reports independently of the
  // userpond filters (from/to/group/scope). Without this the report
  // can stay stale after an entry is reassigned to a different
  // project — the bootstrap effect's deps haven't changed.
  const [reportTick, setReportTick] = useState(0)
  const [err, setErr] = useState<string | null>(null)
  const [byTask, setByTask] = useState<TaskRep[]>([])
  const [openTask, setOpenTask] = useState<string | null>(null)
  const [editId, setEditId] = useState<string | null>(null)
  const [eTask, setETask] = useState('')
  const [eProject, setEProject] = useState('')
  const [eStart, setEStart] = useState('')
  const [eEnd, setEEnd] = useState('')

  const titleOf = (id: string) => tasks.find((x) => x.id === id)?.title ?? id.slice(0, 8)

  const reportQuery = useCallback(() => {
    const q: Record<string, string | boolean> = { group_by: group }
    if (scope === 'mine') q.executor_kind = 'human'
    if (scope === 'ai') q.executor_kind = 'llm_agent'
    if (from) q.start_from = `${from}T00:00:00`
    if (to) q.start_to = `${to}T23:59:59`
    if (billableF !== 'all') q.billable = billableF === 'yes'
    if (clientId) q.client_tag_id = clientId
    if (projectId) q.project_tag_id = projectId
    return q
  }, [group, scope, from, to, billableF, clientId, projectId])

  // Previous-period diff: fetch /time/report grouped by user (gives
  // one row) for the previous range and store totals. Skipped when
  // period=custom (no canonical "previous" of an arbitrary range).
  useEffect(() => {
    if (!periodInfo) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setPrevTotal(null)
      return
    }
    let active = true
    void (async () => {
      const q = { ...reportQuery() }
      q.start_from = `${periodInfo.prevFrom}T00:00:00`
      q.start_to = `${periodInfo.prevTo}T23:59:59`
      q.group_by = 'user'
      const r = await api.GET('/time/report', {
        params: { header: workspaceHeader(), query: q },
      })
      if (!active) return
      if (!r.data || r.data.length === 0) {
        setPrevTotal({ seconds: 0, amount: '0' })
        return
      }
      let secs = 0
      let amount = 0
      for (const row of r.data) {
        secs += row.seconds
        amount += Number(row.amount ?? 0)
      }
      setPrevTotal({ seconds: secs, amount: amount.toFixed(2) })
    })()
    return () => {
      active = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [period, periodAnchor, reportQuery, reportTick])

  // Generic CSV blob download. The two report flavors share auth +
  // filename plumbing; only the path + query differ.
  const downloadCsv = useCallback(
    async (path: string, query: Record<string, string | boolean>, filename: string) => {
      const usp = new URLSearchParams()
      for (const [k, v] of Object.entries(query)) usp.set(k, String(v))
      const res = await authFetch(`${path}?${usp.toString()}`)
      if (!res.ok) {
        setErr(`HTTP ${res.status}`)
        return
      }
      const blob = await res.blob()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = filename
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      URL.revokeObjectURL(url)
    },
    [],
  )

  const downloadReportCsv = useCallback(async () => {
    const today = new Date().toISOString().slice(0, 10)
    await downloadCsv(
      '/time/report.csv',
      reportQuery() as Record<string, string | boolean>,
      `flow-time-report-${today}.csv`,
    )
  }, [downloadCsv, reportQuery])

  const downloadEntriesCsv = useCallback(async () => {
    // Detail CSV: one row per time entry with started_at / ended_at /
    // duration / task / client / project. Uses the same date filters
    // as the report (client/project filters apply on the report side
    // only because /time/entries does not currently take them; if you
    // want a per-client detail CSV, filter on the client column post-
    // hoc in your spreadsheet).
    const q: Record<string, string | boolean> = {}
    if (from) q.start_from = `${from}T00:00:00`
    if (to) q.start_to = `${to}T23:59:59`
    if (billableF !== 'all') q.billable = billableF === 'yes'
    const today = new Date().toISOString().slice(0, 10)
    await downloadCsv('/time/entries.csv', q, `flow-time-entries-${today}.csv`)
  }, [downloadCsv, from, to, billableF])

  const loadReport = useCallback(async () => {
    const r = await api.GET('/time/report', {
      params: { header: workspaceHeader(), query: reportQuery() },
    })
    if (r.data) setReport(r.data)
    const q: Record<string, string> = {}
    if (from) q.start_from = `${from}T00:00:00`
    if (to) q.start_to = `${to}T23:59:59`
    const bt = await api.GET('/time/report/by-task', {
      params: { header: workspaceHeader(), query: q },
    })
    if (bt.data) setByTask(bt.data)
    // Pie chart aggregation: same filters as the main report, just a
    // different group_by. Kept as a separate request so changing the
    // pie selector doesn't force the table to re-render.
    const pieQ = { ...reportQuery(), group_by: pieGroup }
    const pr = await api.GET('/time/report', {
      params: { header: workspaceHeader(), query: pieQ },
    })
    if (pr.data) setPieReport(pr.data)
  }, [reportQuery, from, to, pieGroup])

  const resetEntries = useCallback(async () => {
    const { data } = await api.GET('/time/entries', {
      params: { header: workspaceHeader(), query: { limit: ENT_PAGE, offset: 0 } },
    })
    if (!data) return
    setEntries(data)
    setEntOffset(data.length)
    setEntMore(data.length === ENT_PAGE)
  }, [])

  const loadMore = useCallback(async () => {
    if (entLoading || !entMore) return
    setEntLoading(true)
    const { data } = await api.GET('/time/entries', {
      params: {
        header: workspaceHeader(),
        query: { limit: ENT_PAGE, offset: entOffset },
      },
    })
    setEntLoading(false)
    if (!data) return
    setEntries((p) => [...p, ...data])
    setEntOffset((o) => o + data.length)
    setEntMore(data.length === ENT_PAGE)
  }, [entLoading, entMore, entOffset])

  const reloadEntries = useCallback(async () => {
    await resetEntries()
    await loadReport()
  }, [resetEntries, loadReport])

  // Realtime-ish: poll the running timer (no WS endpoint in v1). State
  // is only set inside the async tick, never sync in the effect body.
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

  useEffect(() => {
    let active = true
    void (async () => {
      const h = workspaceHeader()
      // Workflow states loaded via useWorkflowStates() hook — no
      // need to refetch them here.
      const [tk, cl, pr, pp, allTagsRes] = await Promise.all([
        api.GET('/tasks', { params: { header: h } }),
        api.GET('/tags', { params: { header: h, query: { kind: 'client' } } }),
        api.GET('/tags', { params: { header: h, query: { kind: 'project' } } }),
        api.GET('/projects', { params: { header: h } }),
        api.GET('/tags', { params: { header: h } }),
      ])
      if (!active) return
      if (tk.data) setTasks(tk.data)
      if (cl.data) setClients(cl.data)
      if (pr.data) setProjects(pr.data)
      if (pp.data) setProjectProfiles(pp.data)
      if (allTagsRes.data) setAllTags(allTagsRes.data)
      await resetEntries()
    })()
    return () => {
      active = false
    }
  }, [activeId, resetEntries])

  useEffect(() => {
    let active = true
    void (async () => {
      const q: Record<string, string> = {}
      if (from) q.start_from = `${from}T00:00:00`
      if (to) q.start_to = `${to}T23:59:59`
      const [r, bt] = await Promise.all([
        api.GET('/time/report', {
          params: { header: workspaceHeader(), query: reportQuery() },
        }),
        api.GET('/time/report/by-task', {
          params: { header: workspaceHeader(), query: q },
        }),
      ])
      if (!active) return
      if (r.data) setReport(r.data)
      if (bt.data) setByTask(bt.data)
    })()
    return () => {
      active = false
    }
  }, [activeId, reportQuery, from, to, reportTick])

  // Pie chart fetch (separate from the table report so changing the
  // pie group selector doesn't refetch the table). Also drives the
  // first paint after page load — the previous version only fetched
  // pieReport inside ``loadReport`` (called from edit / create flows),
  // so switching the pie to project or client on a fresh load showed
  // an empty donut.
  useEffect(() => {
    let active = true
    void (async () => {
      if (pieGroup === 'task') return
      const pieQ = { ...reportQuery(), group_by: pieGroup }
      const pr = await api.GET('/time/report', {
        params: { header: workspaceHeader(), query: pieQ },
      })
      if (!active) return
      if (pr.data) setPieReport(pr.data)
    })()
    return () => {
      active = false
    }
  }, [activeId, reportQuery, pieGroup, reportTick])

  const refreshRunning = useCallback(async () => {
    const { data } = await api.GET('/time/running', {
      params: { header: workspaceHeader() },
    })
    setRunning(data ?? [])
  }, [])

  async function beginEdit(en: Entry) {
    setErr(null)
    setEditId(en.id)
    setEProject(en.project_tag_id ?? '')
    setETask(en.task_id)
    setEStart(toLocalInput(en.started_at))
    setEEnd(toLocalInput(en.ended_at))
  }

  async function saveEntry(en: Entry) {
    setErr(null)
    const { error } = await api.PATCH('/time/entries/{entry_id}', {
      params: { header: workspaceHeader(), path: { entry_id: en.id } },
      body: {
        expected_version: en.version,
        task_id: eTask || undefined,
        started_at: eStart ? fromLocalInput(eStart) : undefined,
        ended_at: eEnd ? fromLocalInput(eEnd) : null,
      },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    setEditId(null)
    await resetEntries()
    await loadReport()
    setReportTick((n) => n + 1)
  }

  async function deleteEntry(en: Entry) {
    if (!window.confirm(t('time.confirmDeleteEntry'))) return
    setErr(null)
    const { error } = await api.DELETE('/time/entries/{entry_id}', {
      params: { header: workspaceHeader(), path: { entry_id: en.id } },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    if (editId === en.id) setEditId(null)
    await resetEntries()
    await loadReport()
    setReportTick((n) => n + 1)
  }

  async function startTask(taskId: string, parallel: boolean) {
    if (!taskId) return
    setErr(null)
    const { error } = await api.POST('/time/start', {
      params: { header: workspaceHeader() },
      body: { task_id: taskId, parallel },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    await refreshRunning()
    await reloadEntries()
  }

  async function stopTask(taskId: string) {
    setErr(null)
    const { error } = await api.POST('/time/stop', {
      params: { header: workspaceHeader() },
      body: { task_id: taskId },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    await refreshRunning()
    await reloadEntries()
  }

  async function onStart(e: FormEvent, parallel: boolean) {
    e.preventDefault()
    await startTask(pick, parallel)
  }

  const runningByTask = new Set(running.map((r) => r.task_id))
  const secs = (iso: string) => (now - new Date(iso).getTime()) / 1000
  // Per-task view: drop tasks with no time logged (0% / unreported).
  const shownByTask = byTask.filter((r) => r.total_seconds > 0)

  return (
    <section className="card">
      <h1>{t('time.title')}</h1>
      <p className="hint">{t('time.realtimeNote')}</p>
      {err && <p className="err">{err}</p>}

      <div className="card card--running">
        <h2>{t('time.runningNow')}</h2>
        {running.length === 0 ? (
          <p className="hint">{t('time.idle')}</p>
        ) : (
          <ul className="list">
            {running.map((r) => (
              <li key={r.id} className="taskrow">
                <span className="taskrow__title">
                  {titleOf(r.task_id)}{' '}
                  {r.executor_kind === 'llm_agent' && (
                    <span className="aibadge" title={t('tasks.aiTitle')}>
                      {t('tasks.aiBadge')}
                    </span>
                  )}
                  <span className="chip">
                    {r.parallel ? t('time.parallel') : t('time.serial')}
                  </span>
                </span>
                <span className="taskrow__meta">
                  <strong>{hhmmss(secs(r.started_at))}</strong>
                  <button
                    type="button"
                    className="btn--sm"
                    onClick={() => void stopTask(r.task_id)}
                  >
                    ⏱■ {t('time.stop')}
                  </button>
                </span>
              </li>
            ))}
          </ul>
        )}
        <form onSubmit={(e) => void onStart(e, false)}>
          <label>
            {t('time.pick')}
            <TaskPickList
              tasks={tasks}
              tags={allTags}
              states={wfStates}
              value={pick || null}
              onPick={(id) => setPick(id)}
            />
          </label>
          <div className="row">
            <button type="submit" title={t('time.startSerial')} disabled={!pick}>
              ⏱▶ {t('time.startSerial')}
            </button>
            <button
              type="button"
              className="btn--ghost"
              title={t('time.startParallel')}
              onClick={() => void startTask(pick, true)}
              disabled={!pick}
            >
              ⏱▶▶ {t('time.startParallel')}
            </button>
          </div>
        </form>
      </div>

      <h2>{t('time.entries')}</h2>
      {entries.length === 0 ? (
        <p className="hint">{t('time.none')}</p>
      ) : (
        <div
          className="scrollbox"
          onScroll={(e) => {
            const el = e.currentTarget
            if (
              el.scrollHeight - el.scrollTop - el.clientHeight < 80 &&
              entMore &&
              !entLoading
            ) {
              void loadMore()
            }
          }}
        >
          <ul className="list">
            {entries.map((en) => (
            <li key={en.id} className="taskrow">
              <span className="taskrow__title">
                {en.task_title ?? titleOf(en.task_id)}{' '}
                {en.executor_kind === 'llm_agent' && (
                  <span className="aibadge" title={t('tasks.aiTitle')}>
                    {t('tasks.aiBadge')}
                  </span>
                )}
                <span className="muted">
                  {' '}
                  · {en.duration_seconds != null
                    ? hhmmss(en.duration_seconds)
                    : '...'}
                  {en.billable ? ` · ${t('time.billable')}` : ''}
                </span>
                <span className="muted timewhen">
                  {(en.client_name ?? '—') + ' / ' + (en.project_name ?? '—')}
                  {' · '}
                  {fmtDateTime(en.started_at, en.client_timezone)}–
                  {fmtClockTz(en.ended_at, en.client_timezone)}
                  {en.client_timezone ? ` (${en.client_timezone})` : ''}
                </span>
                {editId === en.id && (
                  <span className="entryedit">
                    <select
                      value={eProject}
                      onChange={(e) => {
                        const newProj = e.target.value
                        setEProject(newProj)
                        // Real bug fix: changing the project dropdown
                        // now ALSO promotes a task in that project as
                        // the new ``eTask`` so the PATCH actually
                        // moves the entry. Without this the dropdown
                        // was a filter-only widget and Save still
                        // sent the old task_id.
                        if (newProj) {
                          const inProj = tasks.find((tk) =>
                            (tk.tags ?? []).some((g) => g.id === newProj),
                          )
                          if (inProj) setETask(inProj.id)
                        }
                      }}
                      title={t('time.editProject')}
                    >
                      <option value="">{t('time.editAnyProject')}</option>
                      {projects.map((p) => (
                        <option key={p.id} value={p.id}>
                          {p.name}
                        </option>
                      ))}
                    </select>
                    <select
                      value={eTask}
                      onChange={(e) => setETask(e.target.value)}
                    >
                      {tasks
                        .filter(
                          (tk) =>
                            !eProject ||
                            (tk.tags ?? []).some((g) => g.id === eProject),
                        )
                        .map((tk) => {
                          const dupe = tasks.filter(
                            (x) => x.title === tk.title && x.id !== tk.id,
                          ).length
                          const suffix = dupe > 0 ? ` · ${tk.id.slice(0, 4)}` : ''
                          return (
                            <option key={tk.id} value={tk.id}>
                              {tk.title}
                              {suffix}
                            </option>
                          )
                        })}
                    </select>
                    <input
                      type="datetime-local"
                      value={eStart}
                      onChange={(e) => setEStart(e.target.value)}
                    />
                    <input
                      type="datetime-local"
                      value={eEnd}
                      onChange={(e) => setEEnd(e.target.value)}
                    />
                    <button
                      type="button"
                      className="btn--sm"
                      onClick={() => void saveEntry(en)}
                    >
                      {t('time.save')}
                    </button>
                    <button
                      type="button"
                      className="btn--ghost btn--sm"
                      onClick={() => setEditId(null)}
                    >
                      {t('notes.close')}
                    </button>
                  </span>
                )}
              </span>
              <span className="taskrow__meta">
                <button
                  type="button"
                  className="btn--ghost btn--sm"
                  onClick={() => void beginEdit(en)}
                >
                  {t('time.editEntry')}
                </button>
                <button
                  type="button"
                  className="btn--ghost btn--sm btn--danger"
                  title={t('time.deleteEntry')}
                  onClick={() => void deleteEntry(en)}
                >
                  {t('time.deleteEntry')}
                </button>
                {runningByTask.has(en.task_id) ? (
                  <button
                    type="button"
                    className="btn--sm"
                    onClick={() => void stopTask(en.task_id)}
                  >
                    ⏱■
                  </button>
                ) : (
                  <>
                    <button
                      type="button"
                      className="btn--ghost btn--sm"
                      title={t('time.startSerial')}
                      onClick={() => void startTask(en.task_id, false)}
                    >
                      ⏱▶
                    </button>
                    <button
                      type="button"
                      className="btn--ghost btn--sm"
                      title={t('time.startParallel')}
                      onClick={() => void startTask(en.task_id, true)}
                    >
                      ⏱▶▶
                    </button>
                  </>
                )}
              </span>
            </li>
          ))}
          </ul>
          {entLoading && <p className="hint">{t('time.loading')}</p>}
        </div>
      )}

      <h2>{t('time.totalByTask')}</h2>
      {shownByTask.length === 0 ? (
        <p className="hint">{t('time.idle')}</p>
      ) : (
        <>
        <div className="viewtabs" role="tablist" aria-label={t('time.pieGroup')}>
          {(['task', 'project', 'client'] as const).map((g) => (
            <button
              key={g}
              type="button"
              role="tab"
              aria-selected={pieGroup === g}
              className={
                'viewtabs__tab' +
                (pieGroup === g ? ' viewtabs__tab--active' : '')
              }
              onClick={() => setPieGroup(g)}
            >
              {t(`time.pieGroup_${g}`)}
            </button>
          ))}
        </div>
        <Donut
          data={(() => {
            // Pie data comes from the dedicated pieReport (grouped by
            // pieGroup) when the user picks project/client; when on
            // 'task' we keep the by-task drill-down so the donut and
            // the table below share the same numbers.
            if (pieGroup === 'task') {
              const s = [...shownByTask].sort(
                (a, b) => b.total_seconds - a.total_seconds,
              )
              const top = s.slice(0, 8).map((r) => ({
                label: r.task_title ?? r.task_id.slice(0, 8),
                secs: r.total_seconds,
              }))
              const rest = s.slice(8).reduce((x, r) => x + r.total_seconds, 0)
              return rest > 0
                ? [...top, { label: `+${s.length - 8}`, secs: rest }]
                : top
            }
            const s = [...pieReport].sort((a, b) => b.seconds - a.seconds)
            const top = s.slice(0, 8).map((r) => ({
              label: r.label ?? '—',
              secs: r.seconds,
            }))
            const rest = s.slice(8).reduce((x, r) => x + r.seconds, 0)
            return rest > 0
              ? [...top, { label: `+${s.length - 8}`, secs: rest }]
              : top
          })()}
        />
        <table className="tbl">
          <thead>
            <tr>
              <th>{t('time.pick')}</th>
              <th>{t('time.client')}</th>
              <th>{t('time.project')}</th>
              <th>{t('time.duration')}</th>
              <th>{t('time.billable')}</th>
              <th>#</th>
            </tr>
          </thead>
          <tbody>
            {shownByTask.map((r) => (
              <Fragment key={r.task_id}>
                <tr
                  className="byrow"
                  onClick={() =>
                    setOpenTask(openTask === r.task_id ? null : r.task_id)
                  }
                >
                  <td>
                    {openTask === r.task_id ? '▾ ' : '▸ '}
                    {r.task_title ?? r.task_id.slice(0, 8)}
                    {/* Disambiguator: when another by-task row has the
                        same title, append a short tail of the UUID so
                        "Stesso task due volte" stops being ambiguous.
                        Pure visual hint; the row is still keyed by
                        task_id and the drill-down stays correct. */}
                    {(() => {
                      const dupe = shownByTask.filter(
                        (x) =>
                          x.task_title === r.task_title &&
                          x.task_id !== r.task_id,
                      ).length
                      return dupe > 0 ? (
                        <span className="muted">
                          {' · '}
                          {r.task_id.slice(0, 4)}
                        </span>
                      ) : null
                    })()}
                  </td>
                  <td>{r.client_name ?? '—'}</td>
                  <td>{r.project_name ?? '—'}</td>
                  <td>{hhmmss(r.total_seconds)}</td>
                  <td>{hhmmss(r.billable_seconds)}</td>
                  <td>{r.entry_count}</td>
                </tr>
                {openTask === r.task_id && (
                  <tr key={r.task_id + '-d'}>
                    <td colSpan={6}>
                      <ul className="list">
                        {entries
                          .filter((e) => e.task_id === r.task_id)
                          .map((e) => (
                            <li key={e.id} className="taskrow">
                              <span className="muted">
                                {fmtDateTime(
                                  e.started_at,
                                  e.client_timezone,
                                )}
                                –
                                {fmtClockTz(e.ended_at, e.client_timezone)}
                                {' · '}
                                {(e.client_name ?? '—') +
                                  ' / ' +
                                  (e.project_name ?? '—')}
                                {e.note_title
                                  ? ` · 📝 ${e.note_title}`
                                  : ''}
                                {' · '}
                                {e.duration_seconds != null
                                  ? hhmmss(e.duration_seconds)
                                  : '…'}
                                {e.billable
                                  ? ` · ${t('time.billable')}`
                                  : ''}
                              </span>
                            </li>
                          ))}
                      </ul>
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
        </>
      )}

      {periodInfo && (
        <div className="perioddiff">
          {(() => {
            // Sum the current period total from ``report`` (already
            // filtered by from/to + reportQuery). The report list
            // groups by ``group`` (project by default); we sum across
            // rows to get the period total regardless of grouping.
            let curSecs = 0
            let curAmount = 0
            for (const row of report) {
              curSecs += row.seconds
              curAmount += Number(row.amount ?? 0)
            }
            const diffSecs = curSecs - (prevTotal?.seconds ?? 0)
            const diffAmount = curAmount - Number(prevTotal?.amount ?? 0)
            const sign = (n: number) => (n > 0 ? '+' : n < 0 ? '−' : '±')
            return (
              <>
                <span className="perioddiff__cur">
                  <strong>{hhmmss(curSecs)}</strong> ·{' '}
                  <strong>{curAmount.toFixed(2)} EUR</strong>
                </span>
                {prevTotal && (
                  <span className="muted perioddiff__delta">
                    {' '}
                    vs {t(`time.period_prev_${period}`)}:{' '}
                    {sign(diffSecs)}
                    {hhmmss(Math.abs(diffSecs))} · {sign(diffAmount)}
                    {Math.abs(diffAmount).toFixed(2)} EUR
                  </span>
                )}
              </>
            )
          })()}
        </div>
      )}

      <h2>
        {t('time.report')}{' '}
        <button
          type="button"
          className="btn--ghost btn--sm"
          onClick={() => void downloadReportCsv()}
          title={t('time.exportCsvAggregatedTip')}
        >
          {t('time.exportCsvAggregated')}
        </button>{' '}
        <button
          type="button"
          className="btn--ghost btn--sm"
          onClick={() => void downloadEntriesCsv()}
          title={t('time.exportCsvDetailedTip')}
        >
          {t('time.exportCsvDetailed')}
        </button>
      </h2>
      <div className="row">
        <label>
          {t('time.groupBy')}
          <select
            value={group}
            onChange={(e) => setGroup(e.target.value as Group)}
          >
            {GROUPS.map((g) => (
              <option key={g} value={g}>
                {t(`time.group.${g}`)}
              </option>
            ))}
          </select>
        </label>
        <label>
          {t('time.scope')}
          <select
            value={scope}
            onChange={(e) => setScope(e.target.value as Scope)}
          >
            <option value="all">{t('graph.scopeAll')}</option>
            <option value="mine">{t('graph.scopeMine')}</option>
            <option value="ai">{t('graph.scopeAi')}</option>
          </select>
        </label>
        <label>
          {t('time.client')}
          <select
            value={clientId}
            onChange={(e) => onPickClient(e.target.value)}
          >
            <option value="">{t('graph.scopeAll')}</option>
            {clients.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          {t('time.project')}
          <select
            value={projectId}
            onChange={(e) => onPickProject(e.target.value)}
          >
            <option value="">{t('graph.scopeAll')}</option>
            {projectsForClient.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        </label>
        <div className="periodbar">
          {(['day', 'week', 'month', 'custom'] as Period[]).map((p) => (
            <button
              key={p}
              type="button"
              className={
                'btn--sm' + (period === p ? '' : ' btn--ghost')
              }
              onClick={() => {
                setPeriod(p)
                if (p !== 'custom') setPeriodAnchor(new Date())
              }}
            >
              {t(`time.period_${p}`)}
            </button>
          ))}
          {periodInfo && (
            <span className="periodbar__nav">
              <button
                type="button"
                className="btn--ghost btn--sm"
                onClick={() => setPeriodAnchor(periodInfo.prevAnchor)}
                title={t('time.periodPrev')}
              >
                ◀
              </button>
              <strong className="periodbar__label">{periodInfo.label}</strong>
              <button
                type="button"
                className="btn--ghost btn--sm"
                onClick={() => setPeriodAnchor(periodInfo.nextAnchor)}
                title={t('time.periodNext')}
              >
                ▶
              </button>
            </span>
          )}
        </div>
        <div className="daterange">
          <label>
            {t('time.from')}
            <input
              type="date"
              value={from}
              onChange={(e) => {
                setPeriod('custom')
                setFrom(e.target.value)
              }}
            />
          </label>
          <label>
            {t('time.to')}
            <input
              type="date"
              value={to}
              onChange={(e) => {
                setPeriod('custom')
                setTo(e.target.value)
              }}
            />
          </label>
        </div>
        <label>
          {t('time.billableFilter')}
          <select
            value={billableF}
            onChange={(e) => setBillableF(e.target.value as BillableF)}
          >
            <option value="all">{t('graph.scopeAll')}</option>
            <option value="yes">{t('time.billableOnly')}</option>
            <option value="no">{t('time.nonBillable')}</option>
          </select>
        </label>
      </div>
      <dl className="kv">
        {report.map((r, i) => (
          <div key={i} style={{ display: 'contents' }}>
            <dt>{r.label ?? r.key ?? '-'}</dt>
            <dd>
              {hhmmss(r.seconds)} · {r.amount} {r.currency}
            </dd>
          </div>
        ))}
      </dl>
    </section>
  )
}
