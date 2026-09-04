import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import { useSession } from '../auth/useSession'
import type { components } from '../shared'

type Calendar = components['schemas']['CalendarOut']
type Task = components['schemas']['TaskOut']
type State = components['schemas']['StateOut']
type Wf = components['schemas']['WorkflowOut']

const WEEK = ['mon', 'tue', 'wed', 'thu', 'fri']

// Newest-first comparator for ISO datetime strings (empty sorts last).
// Both the appointments list and the agenda use it so the most recent
// item is always on top.
function byWhenDesc(a: string, b: string): number {
  if (a === b) return 0
  if (!a) return 1
  if (!b) return -1
  return b.localeCompare(a)
}

// Calendar / appointments view. Appointments live on `tasks` since
// migration 0094 (ADR-0008 addendum): a task with `start_at` +
// `duration_minutes` IS the calendar block, and the GiST EXCLUDE
// constraint enforces no-overlap per `assignee_id`. This route reads
// appointment-tasks (`duration_minutes != null`) and creates them via
// POST /tasks; the working_calendars + holidays UX is unchanged.
export function EventsRoute() {
  const { t } = useTranslation()
  const session = useSession()
  const activeId = session?.workspaceId
  const [cals, setCals] = useState<Calendar[]>([])
  const [tasks, setTasks] = useState<Task[]>([])
  const [calName, setCalName] = useState('')
  const [holCal, setHolCal] = useState('')
  const [holDay, setHolDay] = useState('')
  const [evTitle, setEvTitle] = useState('')
  const [evStart, setEvStart] = useState('')
  const [evEnd, setEvEnd] = useState('')
  const [err, setErr] = useState<string | null>(null)
  // Default-workflow states, fetched once, so terminal (done/cancelled)
  // appointments and reminders can be hidden. They are hidden by default
  // to keep the calendar uncluttered; the toggle opts back into seeing
  // them (off = hidden, matching the user's "enable to view" framing).
  const [wfStates, setWfStates] = useState<State[]>([])
  const [showTerminal, setShowTerminal] = useState(false)

  // Appointment-tasks (start_at + duration_minutes) and reminder /
  // deadline tasks (due_date only) are both fetched from /tasks; the
  // split is done client-side because both share the same row shape.
  const appointmentTasks = tasks.filter((x) => x.duration_minutes != null)
  const dueOnlyTasks = tasks.filter(
    (x) => x.duration_minutes == null && x.due_date != null,
  )

  // Terminal-state filtering. Until the states load `terminalStateIds`
  // is empty, so nothing is hidden (safe default). The toggle is offered
  // only when at least one item is actually terminal — gating on the
  // state set alone would show the pill on every (even empty) calendar,
  // and toggling it would then do nothing visible.
  const terminalStateIds = new Set(
    wfStates.filter((s) => s.is_terminal).map((s) => s.id),
  )
  const isTerminal = (tk: Task) => terminalStateIds.has(tk.state_id)
  const isHidden = (tk: Task) => !showTerminal && isTerminal(tk)
  const hasTerminalTasks =
    appointmentTasks.some(isTerminal) || dueOnlyTasks.some(isTerminal)
  const visibleAppointments = appointmentTasks.filter((tk) => !isHidden(tk))
  const visibleDueTasks = dueOnlyTasks.filter((tk) => !isHidden(tk))

  const reload = useCallback(async () => {
    const h = workspaceHeader()
    const [c, tk] = await Promise.all([
      api.GET('/calendars', { params: { header: h } }),
      api.GET('/tasks', { params: { header: h } }),
    ])
    if (c.data) setCals(c.data)
    if (tk.data) setTasks(tk.data)
  }, [])

  useEffect(() => {
    let active = true
    void (async () => {
      const h = workspaceHeader()
      const [c, tk] = await Promise.all([
        api.GET('/calendars', { params: { header: h } }),
        api.GET('/tasks', { params: { header: h } }),
      ])
      if (!active) return
      if (c.data) setCals(c.data)
      if (tk.data) setTasks(tk.data)
    })()
    return () => {
      active = false
    }
  }, [activeId])

  // Default-workflow states drive the "hide completed" filter. Loaded
  // separately (and only on workspace change) because they never change
  // when an appointment is added or deleted.
  useEffect(() => {
    let active = true
    void (async () => {
      const h = workspaceHeader()
      const wfs = await api.GET('/workflows', { params: { header: h } })
      if (!active || !wfs.data) return
      const def = wfs.data.find((w: Wf) => w.is_default) ?? wfs.data[0]
      if (!def) return
      const st = await api.GET('/workflows/{workflow_id}/states', {
        params: { header: h, path: { workflow_id: def.id } },
      })
      if (!active) return
      if (st.data) setWfStates(st.data)
    })()
    return () => {
      active = false
    }
  }, [activeId])

  async function onAddCal(e: FormEvent) {
    e.preventDefault()
    setErr(null)
    const weekly_hours: Record<string, string[][]> = {}
    for (const d of WEEK) weekly_hours[d] = [['09:00', '17:00']]
    const { error } = await api.POST('/calendars', {
      params: { header: workspaceHeader() },
      body: { name: calName, timezone: 'Europe/Rome', weekly_hours },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    setCalName('')
    await reload()
  }

  async function onAddHoliday(e: FormEvent) {
    e.preventDefault()
    if (!holCal || !holDay) return
    setErr(null)
    const { error } = await api.POST('/calendars/{calendar_id}/holidays', {
      params: { header: workspaceHeader(), path: { calendar_id: holCal } },
      body: { day: holDay },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    setHolDay('')
  }

  // Appointments are created as tasks with start_at + duration_minutes.
  // The duration is derived from the end input so the user still works
  // with the calendar-natural (start, end) pair; the service stores the
  // pair (start_at, duration_minutes) per the migration 0094 contract.
  async function onAddEvent(e: FormEvent) {
    e.preventDefault()
    setErr(null)
    const start = new Date(evStart)
    const end = new Date(evEnd)
    const minutes = Math.round((end.getTime() - start.getTime()) / 60000)
    if (!(minutes > 0)) {
      // The DB CHECK + service validation would catch it, but failing
      // fast keeps the round-trip noise out of the toolbar.
      setErr(t('events.endAfterStart'))
      return
    }
    const { error } = await api.POST('/tasks', {
      params: { header: workspaceHeader() },
      body: {
        title: evTitle,
        // executor_kind/necessity have backend defaults but are written
        // here for the discriminator. importance/urgency/priority are
        // backend-only (Low/Low default, priority derived).
        executor_kind: 'human',
        necessity: 'should',
        start_at: start.toISOString(),
        duration_minutes: minutes,
      },
    })
    if (error) {
      // Overlap for the same assignee -> event.overlap (no ubiquity).
      setErr(errMessage(error))
      return
    }
    setEvTitle('')
    setEvStart('')
    setEvEnd('')
    await reload()
  }

  async function onDelete(id: string) {
    setErr(null)
    // Find current version for the optimistic-concurrency soft delete.
    const tk = appointmentTasks.find((x) => x.id === id)
    if (!tk) return
    const { error } = await api.POST('/tasks/{task_id}/delete', {
      params: { header: workspaceHeader(), path: { task_id: id } },
      body: { expected_version: tk.version },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    await reload()
  }

  return (
    <section className="card">
      <h1>{t('events.title')}</h1>
      {err && <p className="err">{err}</p>}

      <form onSubmit={(e) => void onAddCal(e)} className="row">
        <input
          required
          placeholder={t('events.calName')}
          value={calName}
          onChange={(e) => setCalName(e.target.value)}
        />
        <button type="submit">{t('events.addCal')}</button>
      </form>
      <ul className="list">
        {cals.map((c) => (
          <li key={c.id}>
            {c.name} <span className="muted">· {c.timezone}</span>
          </li>
        ))}
      </ul>

      <form onSubmit={(e) => void onAddHoliday(e)} className="row">
        <select value={holCal} onChange={(e) => setHolCal(e.target.value)}>
          <option value="">--</option>
          {cals.map((c) => (
            <option key={c.id} value={c.id}>
              {c.name}
            </option>
          ))}
        </select>
        <input
          type="date"
          value={holDay}
          onChange={(e) => setHolDay(e.target.value)}
        />
        <button type="submit">{t('events.addHoliday')}</button>
      </form>

      <h2>{t('events.nav')}</h2>
      <p className="hint">{t('events.overlapHint')}</p>
      <form onSubmit={(e) => void onAddEvent(e)} className="row">
        <input
          required
          placeholder={t('events.evTitle')}
          value={evTitle}
          onChange={(e) => setEvTitle(e.target.value)}
        />
        <input
          type="datetime-local"
          required
          value={evStart}
          onChange={(e) => setEvStart(e.target.value)}
        />
        <input
          type="datetime-local"
          required
          value={evEnd}
          onChange={(e) => setEvEnd(e.target.value)}
        />
        <button type="submit">{t('events.addEvent')}</button>
      </form>
      {hasTerminalTasks && (
        <div className="row">
          <button
            type="button"
            role="switch"
            aria-checked={showTerminal}
            className={'toggle-pill' + (showTerminal ? ' toggle-pill--on' : '')}
            onClick={() => setShowTerminal((v) => !v)}
          >
            {t('events.showTerminal')}:{' '}
            {showTerminal ? t('common.on') : t('common.off')}
          </button>
        </div>
      )}
      {visibleAppointments.length === 0 ? (
        <p className="hint">
          {appointmentTasks.length > 0
            ? t('events.allHidden')
            : t('events.none')}
        </p>
      ) : (
        <ul className="list">
          {visibleAppointments
            .slice()
            .sort((a, b) => byWhenDesc(a.start_at ?? '', b.start_at ?? ''))
            .map((ev) => (
              <li key={ev.id}>
                <Link to={`/tasks/${ev.id}`}>{ev.title}</Link>{' '}
                <span className="muted">
                  {ev.start_at?.slice(0, 16)} · {ev.duration_minutes}m
                </span>
                <button type="button" onClick={() => void onDelete(ev.id)}>
                  {t('events.del')}
                </button>
              </li>
            ))}
        </ul>
      )}

      <h2>{t('events.agenda')}</h2>
      <p className="hint">{t('events.agendaHint')}</p>
      {(() => {
        const items = [
          ...visibleAppointments.map((ev) => ({
            key: `e${ev.id}`,
            when: ev.start_at ?? '',
            label: ev.title,
            to: `/tasks/${ev.id}`,
            kind: 'event' as const,
          })),
          ...visibleDueTasks.map((tk) => ({
            key: `t${tk.id}`,
            // Migration 0005: tk.due_date is already an ISO datetime,
            // so the agenda sort can use it directly (no fake T00:00
            // suffix that used to be needed when this was a date).
            when: tk.due_date ?? '',
            label: tk.title,
            to: `/tasks/${tk.id}`,
            kind: 'reminder' as const,
          })),
        ].sort((a, b) => byWhenDesc(a.when, b.when))
        const totalAgenda = appointmentTasks.length + dueOnlyTasks.length
        return items.length === 0 ? (
          <p className="hint">
            {totalAgenda > 0 ? t('events.allHidden') : t('events.none')}
          </p>
        ) : (
          <ul className="list">
            {items.map((it) => (
              <li key={it.key}>
                <span className="muted">{it.when.slice(0, 16)}</span>{' '}
                <Link to={it.to}>
                  {it.kind === 'event' ? '🕒' : '📅'} {it.label}
                </Link>
              </li>
            ))}
          </ul>
        )
      })()}
    </section>
  )
}
