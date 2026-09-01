import {
  Fragment,
  useCallback,
  useEffect,
  useId,
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
import { enumerateDays, periodRange, type Period } from '../lib/period'
import { PeriodPicker } from '../components/PeriodPicker'
import { TaskPickList } from '../components/TaskPickList'
import { DayBars, Donut } from '../components/TimeCharts'
import {
  REST_KEY,
  useColorOf,
  type ChartSeries,
  type DayBucket,
} from '../components/timeChartColors'
import { activeElapsedSec, hhmm, hhmmss, isPaused } from '../lib/time'
import { refreshRunning, useRunningTimers } from '../lib/useRunningTimer'
import type { components, paths } from '../api/schema'

type Task = components['schemas']['TaskOut']
type Entry = components['schemas']['TimeEntryOut']
type TaskRep = components['schemas']['TaskTimeReportOut']
type Row = components['schemas']['ReportRowOut']
type Group = components['schemas']['ReportGroup']
type Tag = components['schemas']['TagOut']
type Project = components['schemas']['ProjectOut']
type Scope = 'all' | 'mine' | 'ai'
type BillableF = 'all' | 'yes' | 'no'

const GROUPS: Group[] = ['project', 'client', 'generic', 'user', 'task', 'task_memo']
const ENT_PAGE = 50
// Report buckets key on null for "unassigned" (an entry whose task carries
// no client / project tag). Give it a stable string so it can hold a
// colour and a legend row like any other bucket. The leading space keeps
// it (and REST_KEY) collision-free against a UUID or a tag name.
const NO_KEY = ' none'

// One (day, bucket) cell: the tracked time and the money it bills. Held
// together because the histogram and the billing table below it must never
// be able to disagree about the same cell.
type DayCell = { secs: number; amount: number }

// GET /time/report/daily row: a ReportRowOut plus the calendar day it
// falls on (``day`` is an ISO YYYY-MM-DD in the timezone the request
// asked for, which is what makes it a valid key into the zero-filled
// day map below).
type DailyRow = components['schemas']['DailyReportRowOut']

// The three chart/report queries are typed from the GENERATED operations,
// not as a hand-rolled `Record<string, string | boolean>`. That matters:
// an index-signature Record is assignable to every operation's params, so
// a filter key that is misspelled — or quietly dropped, which is exactly
// the bug this view shipped with — type-checks clean and is then ignored
// by the server. Naming the operation types makes the compiler the drift
// guard: the report, the donut, the histogram and the by-task table can
// no longer diverge on a param name without failing `npm run typecheck`.
type ReportQuery = NonNullable<
  paths['/time/report']['get']['parameters']['query']
>
type ByTaskQuery = NonNullable<
  paths['/time/report/by-task']['get']['parameters']['query']
>
type DailyQuery = NonNullable<
  paths['/time/report/daily']['get']['parameters']['query']
>

export function TimeRoute() {
  const { t } = useTranslation()
  const session = useSession()
  const activeId = session?.workspaceId
  const [tasks, setTasks] = useState<Task[]>([])
  // "What is running now" is read from the shared source, not kept here:
  // this view and the top-bar chip are two renderings of one fact, and
  // while each held its own copy (its own poll, reconciled only by its
  // own mutations) a timer started here was missing from the chip until
  // the chip's next backstop poll.
  const { running, known: runningKnown, now } = useRunningTimers()
  const [entries, setEntries] = useState<Entry[]>([])
  const [entOffset, setEntOffset] = useState(0)
  const [entMore, setEntMore] = useState(false)
  const [entLoading, setEntLoading] = useState(false)
  const [report, setReport] = useState<Row[]>([])
  const [group, setGroup] = useState<Group>('project')
  // The CHARTS (donut + per-day histogram) are grouped independently
  // from the report table — three buckets only: task / project / client.
  // One selector drives both, so they are always two views of the same
  // series list. Persisted in localStorage (key unchanged); default =
  // project.
  type PieGroup = 'task' | 'project' | 'client'
  const PIE_KEY = 'mycelium.time.pieGroup'
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
  // Per-day rows behind the histogram. Same filters and same group_by as
  // the donut; only the extra day axis differs.
  const [daily, setDaily] = useState<DailyRow[]>([])
  // One legend labels BOTH charts, so both reference it by id.
  const legendId = useId()
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
  const [eMemo, setEMemo] = useState('')

  const titleOf = (id: string) => tasks.find((x) => x.id === id)?.title ?? id.slice(0, 8)

  const reportQuery = useCallback((): ReportQuery => {
    const q: ReportQuery = { group_by: group }
    if (scope === 'mine') q.executor_kind = 'human'
    if (scope === 'ai') q.executor_kind = 'llm_agent'
    if (from) q.start_from = `${from}T00:00:00`
    if (to) q.start_to = `${to}T23:59:59`
    if (billableF !== 'all') q.billable = billableF === 'yes'
    if (clientId) q.client_tag_id = clientId
    if (projectId) q.project_tag_id = projectId
    return q
  }, [group, scope, from, to, billableF, clientId, projectId])

  // /time/report/by-task takes the same filter knobs as /time/report, it
  // just has no group_by (the grouping IS the task). Previously this call
  // sent start_from/start_to ONLY, so picking a client — or a project, or
  // a scope, or billable-only — narrowed everything on screen EXCEPT the
  // "By task" table and the charts while the tab strip sat on "task",
  // which showed the whole workspace next to a filtered report.
  const byTaskQuery = useCallback((): ByTaskQuery => {
    // group_by is the ONE knob /time/report/by-task does not take (the
    // grouping IS the task); every other filter is shared verbatim.
    const q: ReportQuery = { ...reportQuery() }
    delete q.group_by
    return q
  }, [reportQuery])

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
    async (
      path: string,
      // Widened to the generated query shapes (whose values are
      // ``T | null | undefined``) so the callers can hand over the very
      // object they send to the JSON endpoint instead of casting it. An
      // absent filter is dropped rather than serialised as the string
      // "undefined", which the server would reject.
      query: Record<string, string | number | boolean | null | undefined>,
      filename: string,
    ) => {
      const usp = new URLSearchParams()
      for (const [k, v] of Object.entries(query)) {
        if (v !== null && v !== undefined) usp.set(k, String(v))
      }
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
    // /time/report.csv takes exactly /time/report's params, so the CSV is
    // the same rows the table shows — no cast needed to say so.
    await downloadCsv(
      '/time/report.csv',
      reportQuery(),
      `mycelium-time-report-${today}.csv`,
    )
  }, [downloadCsv, reportQuery])

  const downloadEntriesCsv = useCallback(async () => {
    // Detail CSV: one row per time entry with started_at / ended_at /
    // duration / task / client / project. Honours the same client /
    // project focus as the on-screen Entries list and the report so a
    // CSV export of /time matches what's visible above the button.
    const q: Record<string, string | boolean> = {}
    if (from) q.start_from = `${from}T00:00:00`
    if (to) q.start_to = `${to}T23:59:59`
    if (billableF !== 'all') q.billable = billableF === 'yes'
    if (clientId) q.client_tag_id = clientId
    if (projectId) q.project_tag_id = projectId
    const today = new Date().toISOString().slice(0, 10)
    await downloadCsv('/time/entries.csv', q, `mycelium-time-entries-${today}.csv`)
  }, [downloadCsv, from, to, billableF, clientId, projectId])

  // Entries query reads the same focus (client + project) as the
  // report / pie / by-task pulls. Without these the Entries list
  // ignored the focus selector entirely and showed unrelated rows
  // alongside the filtered report — reported as "Entries doesn't
  // refilter when I change focus".
  const entriesQuery = useCallback((): Record<string, string | number> => {
    const q: Record<string, string | number> = { limit: ENT_PAGE }
    if (clientId) q.client_tag_id = clientId
    if (projectId) q.project_tag_id = projectId
    return q
  }, [clientId, projectId])

  const resetEntries = useCallback(async () => {
    const { data } = await api.GET('/time/entries', {
      params: { header: workspaceHeader(), query: { ...entriesQuery(), offset: 0 } },
    })
    if (!data) return
    setEntries(data)
    setEntOffset(data.length)
    setEntMore(data.length === ENT_PAGE)
  }, [entriesQuery])

  const loadMore = useCallback(async () => {
    if (entLoading || !entMore) return
    setEntLoading(true)
    const { data } = await api.GET('/time/entries', {
      params: {
        header: workspaceHeader(),
        query: { ...entriesQuery(), offset: entOffset },
      },
    })
    setEntLoading(false)
    if (!data) return
    setEntries((p) => [...p, ...data])
    setEntOffset((o) => o + data.length)
    setEntMore(data.length === ENT_PAGE)
  }, [entLoading, entMore, entOffset, entriesQuery])

  // Every mutation funnels through here, and every derived read (report,
  // by-task, donut, per-day histogram, previous-period diff) hangs off the
  // SAME ``reportTick`` signal. There used to be a second, imperative
  // ``loadReport`` refreshing three of those four by hand: it predated the
  // histogram and never learned about it, so start / stop / pause grew the
  // donut while the columns underneath it kept the old heights and the two
  // stopped summing to the same total. A partial refresher is a bug that
  // reappears with every new panel — refetch by signal, not by enumeration.
  const reloadEntries = useCallback(async () => {
    await resetEntries()
    setReportTick((n) => n + 1)
  }, [resetEntries])

  // Bootstrap data (tasks, clients/projects tags, projects, all tags)
  // only needs to be (re)loaded when the workspace changes, not every
  // time the focus selector flips — keep this effect keyed on
  // ``activeId`` only and let a second effect refresh the entries list
  // when the focus changes.
  useEffect(() => {
    let active = true
    void (async () => {
      const h = workspaceHeader()
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
    })()
    return () => {
      active = false
    }
  }, [activeId])

  // Refresh the Entries list whenever the focus filter changes.
  // resetEntries closes over clientId / projectId, so this re-fires
  // when the focus selector flips. Without this, the Entries list
  // showed stale rows that didn't match the focus above it. The
  // setState happens inside resetEntries' async IIFE, not directly
  // in the effect body — same pattern used by the report/by-task
  // effects above (avoids the cascading-renders lint).
  useEffect(() => {
    let active = true
    void (async () => {
      if (!active) return
      await resetEntries()
    })()
    return () => {
      active = false
    }
  }, [resetEntries])

  useEffect(() => {
    let active = true
    void (async () => {
      const [r, bt] = await Promise.all([
        api.GET('/time/report', {
          params: { header: workspaceHeader(), query: reportQuery() },
        }),
        api.GET('/time/report/by-task', {
          params: { header: workspaceHeader(), query: byTaskQuery() },
        }),
      ])
      if (!active) return
      if (r.data) setReport(r.data)
      if (bt.data) setByTask(bt.data)
    })()
    return () => {
      active = false
    }
  }, [activeId, reportQuery, byTaskQuery, reportTick])

  // Chart aggregation fetch (separate from the table report so changing
  // the chart group selector doesn't refetch the table). Also drives the
  // first paint after page load — the previous version only fetched
  // pieReport from the imperative refresher called by the edit / create
  // flows, so switching the charts to project or client on a fresh load
  // showed an empty donut. It now runs for 'task' too: the donut used to read
  // the by-task report for that one tab, which meant the two charts
  // could not share a bucket key (/time/report/daily buckets by the
  // report's keys), and the numbers came from a different query.
  useEffect(() => {
    let active = true
    void (async () => {
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

  // The per-day query, shared by the histogram fetch and the CSV export so
  // the file can never be a different cut of the data than the chart and
  // the table the operator was looking at when they clicked Export.
  const dailyQuery = useCallback((): DailyQuery => {
    const q: DailyQuery = { ...reportQuery(), group_by: pieGroup }
    // The server validates the zone and 4xx's on an unknown one rather
    // than silently bucketing in UTC, so send it only when the browser
    // actually resolved one.
    const tz = Intl.DateTimeFormat().resolvedOptions().timeZone
    if (tz) q.tz = tz
    return q
  }, [reportQuery, pieGroup])

  const downloadDailyCsv = useCallback(async () => {
    // The billing export: one row per (day, client|project|task) with
    // seconds, hours, billable seconds and amount. /time/report.csv
    // collapses the whole period into a single row per bucket, which cannot
    // itemise an invoice by day. Same query as the chart above it.
    const today = new Date().toISOString().slice(0, 10)
    await downloadCsv(
      '/time/report/daily.csv',
      dailyQuery(),
      `mycelium-time-daily-${pieGroup}-${today}.csv`,
    )
  }, [downloadCsv, dailyQuery, pieGroup])

  // Per-day histogram fetch. Keyed exactly like the donut's (plus the
  // browser's IANA zone, so "a day" is the user's day and not UTC's), and
  // it carries reportTick so an entry edit / delete refreshes it too.
  useEffect(() => {
    let active = true
    void (async () => {
      const q = dailyQuery()
      let rows: DailyRow[] | undefined
      try {
        const r = await api.GET('/time/report/daily', {
          params: { header: workspaceHeader(), query: q },
        })
        rows = r.data
      } catch {
        // Network error: fall through with rows === undefined.
      }
      if (!active) return
      // Only a SUCCESSFUL response replaces the render (an empty array
      // still counts, and still clears the chart). A failed refetch keeps
      // the frame — blanking the histogram on a transient 500 is a worse
      // lie than showing the previous (still-labelled) slice.
      if (rows) setDaily(rows)
    })()
    return () => {
      active = false
    }
  }, [activeId, dailyQuery, reportTick])

  // Drill-down under a "By task" row. This used to filter the already
  // loaded `entries` array client-side, which showed entries from OUTSIDE
  // the selected period and silently missed everything past the first
  // page of 50. Fetch the task's entries instead, under the same period /
  // billable / client / project filters as the report above it. The row
  // is keyed by task id so a stale payload never paints under a different
  // task while the new one is in flight.
  const [drill, setDrill] = useState<{ taskId: string; rows: Entry[] } | null>(
    null,
  )
  useEffect(() => {
    let active = true
    void (async () => {
      if (!openTask) {
        setDrill(null)
        return
      }
      const q: Record<string, string | number | boolean> = {
        task_id: openTask,
        limit: 500,
      }
      if (from) q.start_from = `${from}T00:00:00`
      if (to) q.start_to = `${to}T23:59:59`
      if (billableF !== 'all') q.billable = billableF === 'yes'
      if (clientId) q.client_tag_id = clientId
      if (projectId) q.project_tag_id = projectId
      const { data } = await api.GET('/time/entries', {
        params: { header: workspaceHeader(), query: q },
      })
      if (!active) return
      setDrill({ taskId: openTask, rows: data ?? [] })
    })()
    return () => {
      active = false
    }
  }, [
    activeId,
    openTask,
    from,
    to,
    billableF,
    clientId,
    projectId,
    reportTick,
  ])

  async function beginEdit(en: Entry) {
    setErr(null)
    setEditId(en.id)
    setEProject(en.project_tag_id ?? '')
    setETask(en.task_id)
    setEStart(toLocalInput(en.started_at))
    setEEnd(toLocalInput(en.ended_at))
    setEMemo(en.memo ?? '')
  }

  async function saveEntry(en: Entry) {
    setErr(null)
    // A time entry belongs to a task. The project a row reports under
    // is *derived* from that task (the task's earliest project tag,
    // resolved server-side). So to "move an entry to a different
    // project" the user must pick a task that lives in that project —
    // the Project dropdown above only filters the Task choices, it
    // cannot itself set the project. The previous implementation tried
    // to fake direct project assignment by auto-promoting "the first
    // task with this project tag" on project-dropdown change, which
    // failed silently when:
    //   - no task in the picked project existed (PATCH still carried
    //     the old task_id and nothing visibly changed);
    //   - the auto-promoted task belonged to a different client.
    // The fix is to require an explicit task pick. If the user blanks
    // the task, we refuse to save and surface a clear error instead of
    // sending a no-op PATCH. Reported repeatedly as "I change the
    // project, press Save, and nothing happens".
    if (!eTask) {
      setErr(t('time.errPickTask'))
      return
    }
    const { error } = await api.PATCH('/time/entries/{entry_id}', {
      params: { header: workspaceHeader(), path: { entry_id: en.id } },
      body: {
        expected_version: en.version,
        task_id: eTask,
        started_at: eStart ? fromLocalInput(eStart) : undefined,
        ended_at: eEnd ? fromLocalInput(eEnd) : null,
        // exclude_unset on the server: send memo explicitly so an edit
        // (or a clear, via null) takes effect.
        memo: eMemo.trim() || null,
      },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    setEditId(null)
    await reloadEntries()
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
    await reloadEntries()
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

  // Pause freezes a running entry (server banks the elapsed); resume
  // reopens a live segment. The entry stays open either way.
  async function pauseResumeTask(taskId: string, paused: boolean) {
    setErr(null)
    const { error } = await api.POST(paused ? '/time/resume' : '/time/pause', {
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

  const runningByTask = new Map(running.map((r) => [r.task_id, r]))
  // Per-task view: drop tasks with no time logged (0% / unreported).
  const shownByTask = byTask.filter((r) => r.total_seconds > 0)

  // ---- ONE series list, shared by both charts -------------------------
  // Top 8 buckets by tracked time, then the whole tail folded into a
  // single "+N" residual (a 9th categorical hue is never invented). The
  // donut, the histogram and the legend all read THIS array and the
  // key -> colour map built from it, so a colour means the same entity in
  // both charts and dropping a bucket never repaints the survivors.
  const chartRows = [...pieReport]
    .filter((r) => r.seconds > 0)
    .sort(
      (a, b) =>
        b.seconds - a.seconds || (a.label ?? '').localeCompare(b.label ?? ''),
    )
  const topRows = chartRows.slice(0, 8)
  const restSecs = chartRows.slice(8).reduce((s, r) => s + r.seconds, 0)
  const chartSeries: ChartSeries[] = [
    ...topRows.map((r) => ({
      key: r.key ?? NO_KEY,
      label: r.label ?? r.key?.slice(0, 8) ?? '—',
      secs: r.seconds,
    })),
    ...(restSecs > 0
      ? [
          {
            key: REST_KEY,
            label: `+${chartRows.length - 8}`,
            secs: restSecs,
          },
        ]
      : []),
  ]
  const colorOf = useColorOf(chartSeries)

  // Zero-fill the histogram across the SELECTED range: /time/report/daily
  // only returns (day, bucket) pairs that have time, and a day with none
  // has to read as "no work" rather than disappear from the axis. Buckets
  // outside the top 8 fold into the same residual the donut uses — both
  // endpoints run the same server-side bucketer over the same filtered
  // entries, so every key here also exists in chartRows.
  //
  // The day map is then WIDENED by whatever the server actually returned,
  // and that is not belt-and-braces. start_from / start_to select
  // instants, while the histogram buckets in the browser's zone: east of
  // UTC an entry started late on the range's last day belongs to the NEXT
  // calendar day, and west of UTC one started early on the first day
  // belongs to the PREVIOUS one. Those rows are part of the very report
  // the donut draws (per-day sums equal report() totals by construction),
  // so dropping them as "outside the range" is precisely how the two
  // charts end up printing different totals for the same filters —
  // verified against the API: a 30-minute entry at 23:30Z on the last day
  // of a month showed 30m in the donut and an empty histogram for a
  // UTC+2 browser. Better a column one day past the picker than a chart
  // that quietly disagrees with the one above it.
  //
  // Seconds AND amount are accumulated into ONE structure, not two parallel
  // maps: the chart reads the seconds, the billing table reads both, and a
  // second structure fed by a second pass is exactly how the donut and the
  // histogram drifted apart before.
  const topKeys = new Set(topRows.map((r) => r.key ?? NO_KEY))
  const dayCells = new Map<string, Record<string, DayCell>>()
  for (const d of enumerateDays(from, to)) dayCells.set(d, {})
  for (const row of daily) {
    const cells = dayCells.get(row.day) ?? {}
    dayCells.set(row.day, cells)
    const k = row.key ?? NO_KEY
    const bucket = topKeys.has(k) ? k : REST_KEY
    const cur = cells[bucket] ?? { secs: 0, amount: 0 }
    cells[bucket] = {
      secs: cur.secs + row.seconds,
      amount: cur.amount + Number(row.amount ?? 0),
    }
  }
  // ISO YYYY-MM-DD sorts lexicographically = chronologically, so the axis
  // stays in order after the widening appended an out-of-range day.
  const dayOrder = [...dayCells.keys()].sort((a, b) =>
    a < b ? -1 : a > b ? 1 : 0,
  )
  const dayBuckets: DayBucket[] = dayOrder.map((day) => ({
    day,
    parts: Object.fromEntries(
      Object.entries(dayCells.get(day) ?? {}).map(([k, c]) => [k, c.secs]),
    ),
  }))

  // ---- The billing table ----------------------------------------------
  // The histogram answers "how did the month go"; an invoice needs the
  // exact figure per day per client/project, which is not something anyone
  // should be reading off a bar. Same buckets, same colours, same filters —
  // only days that actually carry time (a zero row adds nothing to an
  // invoice, whereas in the chart above an empty column IS information).
  const billRows = dayOrder
    .map((day) => {
      const cells = dayCells.get(day) ?? {}
      const vals = Object.values(cells)
      return {
        day,
        cells,
        secs: vals.reduce((s, c) => s + c.secs, 0),
        amount: vals.reduce((s, c) => s + c.amount, 0),
      }
    })
    .filter((r) => r.secs > 0)
  const billTotals = chartSeries.map((s) =>
    billRows.reduce((acc, r) => acc + (r.cells[s.key]?.secs ?? 0), 0),
  )
  const billGrandSecs = billRows.reduce((a, r) => a + r.secs, 0)
  const billGrandAmount = billRows.reduce((a, r) => a + r.amount, 0)
  // Currency is per bucket server-side; the page has always summed across
  // them (see the period diff), so report the one actually carrying money.
  const billCurrency = daily.find((r) => Number(r.amount ?? 0) > 0)?.currency ?? ''

  return (
    <section className="card">
      <h1>{t('time.title')}</h1>
      <p className="hint">{t('time.realtimeNote')}</p>
      {err && <p className="err">{err}</p>}

      <div className="card card--running">
        <h2>{t('time.runningNow')}</h2>
        {/* "Not read yet" is not "nothing is running": until the first
            successful read lands (or after a workspace switch, which
            resets the store) the card says so instead of asserting an
            idle state it has not checked. */}
        {!runningKnown ? (
          <p className="hint">{t('common.loading')}</p>
        ) : running.length === 0 ? (
          <p className="hint">{t('time.idle')}</p>
        ) : (
          <ul className="list">
            {running.map((r) => (
              <li key={r.id} className="taskrow">
                <span className="taskrow__title">
                  {r.task_title ?? titleOf(r.task_id)}{' '}
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
                  <strong className={isPaused(r) ? 'is-paused' : undefined}>
                    {hhmmss(activeElapsedSec(r, now))}
                  </strong>
                  <button
                    type="button"
                    className="btn--ghost btn--sm"
                    onClick={() => void pauseResumeTask(r.task_id, isPaused(r))}
                  >
                    {isPaused(r)
                      ? `⏱▶ ${t('time.resume')}`
                      : `⏱⏸ ${t('time.pause')}`}
                  </button>
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
                {en.memo && editId !== en.id && (
                  <span className="muted entryrow__memo" title={en.memo}>
                    {' '}
                    ✎ {en.memo}
                  </span>
                )}
                {editId === en.id && (() => {
                  // Project dropdown = pure filter for the Task select
                  // below. Picking a project narrows the Task choices;
                  // the Task select is the real control that moves
                  // the entry. When the project filter changes such
                  // that the current task no longer matches, blank
                  // eTask so the Save button surfaces the "pick a
                  // task" guard rather than silently re-saving the
                  // old task_id. The empty-task state is also flagged
                  // visually (red Task select + inline warning) so
                  // imported entries whose chosen project has zero
                  // candidate tasks don't look "silently broken".
                  const tasksInProj = tasks.filter(
                    (tk) => !eProject || (tk.tags ?? []).some((g) => g.id === eProject),
                  )
                  const taskInvalid = !eTask
                  const noTaskInProj = !!eProject && tasksInProj.length === 0
                  return (
                    <form
                      className="entryedit"
                      onSubmit={(e) => {
                        e.preventDefault()
                        void saveEntry(en)
                      }}
                    >
                      <select
                        value={eProject}
                        onChange={(e) => {
                          const newProj = e.target.value
                          setEProject(newProj)
                          if (newProj) {
                            const cur = tasks.find((tk) => tk.id === eTask)
                            const stillFits =
                              cur &&
                              (cur.tags ?? []).some((g) => g.id === newProj)
                            if (!stillFits) setETask('')
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
                        aria-invalid={taskInvalid}
                        className={
                          taskInvalid ? 'entryedit__select--invalid' : undefined
                        }
                        title={
                          taskInvalid
                            ? noTaskInProj
                              ? t('time.editNoTaskInProjectHint')
                              : t('time.editPickTaskHint')
                            : undefined
                        }
                      >
                        <option value="">
                          {tasksInProj.length === 0
                            ? t('time.editNoTaskInProject')
                            : t('time.editPickTask')}
                        </option>
                        {tasksInProj.map((tk) => {
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
                      <input
                        type="text"
                        className="entryedit__memo"
                        value={eMemo}
                        onChange={(e) => setEMemo(e.target.value)}
                        placeholder={t('time.memoPlaceholder')}
                        title={t('time.memoEdit')}
                      />
                      <button
                        type="submit"
                        className="btn--sm"
                        disabled={taskInvalid}
                        title={
                          taskInvalid ? t('time.errPickTask') : undefined
                        }
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
                      {taskInvalid && (
                        <span className="entryedit__warn" role="alert">
                          {noTaskInProj
                            ? t('time.editNoTaskInProjectHint')
                            : t('time.editPickTaskHint')}
                        </span>
                      )}
                    </form>
                  )
                })()}
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
                  (() => {
                    const live = runningByTask.get(en.task_id)
                    const paused = !!live && isPaused(live)
                    return (
                      <>
                        <button
                          type="button"
                          className="btn--ghost btn--sm"
                          title={paused ? t('time.resume') : t('time.pause')}
                          aria-label={paused ? t('time.resume') : t('time.pause')}
                          onClick={() =>
                            void pauseResumeTask(en.task_id, paused)
                          }
                        >
                          {paused ? '⏱▶' : '⏱⏸'}
                        </button>
                        <button
                          type="button"
                          className="btn--sm"
                          title={t('time.stop')}
                          aria-label={t('time.stop')}
                          onClick={() => void stopTask(en.task_id)}
                        >
                          ⏱■
                        </button>
                      </>
                    )
                  })()
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
          series={chartSeries}
          colorOf={colorOf}
          legendId={legendId}
        />
        <h3>
          {t('time.byDay')}{' '}
          <button
            type="button"
            className="btn--ghost btn--sm"
            onClick={() => void downloadDailyCsv()}
            title={t('time.exportCsvDailyTip')}
          >
            {t('time.exportCsvDaily')}
          </button>
        </h3>
        <DayBars
          days={dayBuckets}
          series={chartSeries}
          colorOf={colorOf}
          legendId={legendId}
        />
        {billRows.length > 0 && (
          <>
          {/* The hint lives OUTSIDE the scroll box: as a <caption> it
              inherited the table's nowrap and got clipped, and it scrolled
              away horizontally with the columns it describes. */}
          <p className="hint" id={`${legendId}-daytbl`}>
            {t('time.byDayTableHint')}
          </p>
          <div className="scrollbox scrollbox--x">
            <table
              className="tbl daytbl"
              aria-describedby={`${legendId}-daytbl`}
            >
              <thead>
                <tr>
                  <th scope="col">{t('time.day')}</th>
                  {chartSeries.map((s) => (
                    <th key={s.key} scope="col" className="num">
                      <span
                        className="pieswatch"
                        style={{ background: colorOf(s.key) }}
                        aria-hidden="true"
                      />
                      {s.label}
                    </th>
                  ))}
                  <th scope="col" className="num">
                    {t('time.duration')}
                  </th>
                  <th scope="col" className="num">
                    {t('time.amount')}
                  </th>
                </tr>
              </thead>
              <tbody>
                {billRows.map((r) => (
                  <tr key={r.day}>
                    <th scope="row">{r.day}</th>
                    {chartSeries.map((s) => {
                      const c = r.cells[s.key]
                      return (
                        <td key={s.key} className="num">
                          {c && c.secs > 0 ? (
                            hhmm(c.secs)
                          ) : (
                            <span className="muted">·</span>
                          )}
                        </td>
                      )
                    })}
                    <td className="num">
                      <strong>{hhmm(r.secs)}</strong>
                    </td>
                    <td className="num">
                      {r.amount.toFixed(2)} {billCurrency}
                    </td>
                  </tr>
                ))}
              </tbody>
              <tfoot>
                <tr>
                  <th scope="row">{t('time.total')}</th>
                  {billTotals.map((secs, i) => (
                    <td key={chartSeries[i].key} className="num">
                      {secs > 0 ? hhmm(secs) : <span className="muted">·</span>}
                    </td>
                  ))}
                  <td className="num">
                    <strong>{hhmm(billGrandSecs)}</strong>
                  </td>
                  <td className="num">
                    <strong>
                      {billGrandAmount.toFixed(2)} {billCurrency}
                    </strong>
                  </td>
                </tr>
              </tfoot>
            </table>
          </div>
          </>
        )}
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
                      {drill?.taskId !== r.task_id ? (
                        <p className="hint">{t('time.drillLoading')}</p>
                      ) : drill.rows.length === 0 ? (
                        <p className="hint">{t('time.drillNone')}</p>
                      ) : (
                        <ul className="list">
                          {drill.rows.map((e) => (
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
                      )}
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
            const prevSecs = prevTotal?.seconds ?? 0
            const prevAmount = Number(prevTotal?.amount ?? 0)
            const diffSecs = curSecs - prevSecs
            const diffAmount = curAmount - prevAmount
            const sign = (n: number) => (n > 0 ? '+' : n < 0 ? '−' : '±')
            return (
              <>
                <span className="perioddiff__cur">
                  <strong>{hhmmss(curSecs)}</strong> ·{' '}
                  <strong>{curAmount.toFixed(2)} EUR</strong>
                </span>
                {prevTotal && (
                  // Show the previous period's ABSOLUTE total first, then
                  // the delta in parentheses. The old "vs last week: +X"
                  // form was a bare delta: when last week was empty it
                  // read as +current and looked identical to this week.
                  <span className="muted perioddiff__delta">
                    {' '}
                    {t(`time.period_prev_${period}`)}: {hhmmss(prevSecs)} ·{' '}
                    {prevAmount.toFixed(2)} EUR ({sign(diffSecs)}
                    {hhmmss(Math.abs(diffSecs))} · {sign(diffAmount)}
                    {Math.abs(diffAmount).toFixed(2)} EUR)
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
        <PeriodPicker
          period={period}
          anchor={periodAnchor}
          onChange={(p, a) => {
            setPeriod(p)
            setPeriodAnchor(a)
          }}
        />
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
            <dt className="reportrow__label" title={r.label ?? undefined}>
              {r.label ?? r.key ?? '-'}
            </dt>
            <dd>
              {hhmmss(r.seconds)} · {r.amount} {r.currency}
            </dd>
          </div>
        ))}
      </dl>
    </section>
  )
}
