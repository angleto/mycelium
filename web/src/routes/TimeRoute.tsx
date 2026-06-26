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
import { useIsDark } from '../lib/useIsDark'
import { useWorkflowStates } from '../lib/useWorkflowStates'
import { periodRange, type Period } from '../lib/period'
import { PeriodPicker } from '../components/PeriodPicker'
import { TaskPickList } from '../components/TaskPickList'
import { activeElapsedSec, isPaused } from '../lib/time'
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

const GROUPS: Group[] = ['project', 'client', 'generic', 'user', 'task', 'task_memo']
const ENT_PAGE = 50

function hhmmss(sec: number): string {
  const s = Math.max(0, Math.floor(sec))
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  return `${h}h ${String(m).padStart(2, '0')}m ${String(s % 60).padStart(2, '0')}s`
}

// Categorical donut palette. A fixed set cannot read against both a
// near-white and a near-black surface, so there are two hand-tuned ramps
// (each entry >=3:1 against its own surface for the data-viz boundary)
// and the donut picks one from the resolved theme.
const PIE_LIGHT = [
  '#4a6b3e',
  '#5b3fb8',
  '#6a4f33',
  '#a8456f',
  '#0369a1',
  '#a13322',
  '#b45309',
  '#3f6b32',
  '#0891b2',
]
const PIE_DARK = [
  '#7fa56e',
  '#9a82e0',
  '#a98963',
  '#d99cb8',
  '#38bdf8',
  '#f08a76',
  '#d97706',
  '#a8d49a',
  '#22d3ee',
]

// Dependency-free SVG donut (stroke-dasharray technique) of the
// time distribution per slice, with a legend.
function Donut({ data }: { data: { label: string; secs: number }[] }) {
  const PIE = useIsDark() ? PIE_DARK : PIE_LIGHT
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
  const [eMemo, setEMemo] = useState('')

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
