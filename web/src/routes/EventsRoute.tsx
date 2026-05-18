import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import { useSession } from '../auth/useSession'
import type { components } from '../api/schema'

type Calendar = components['schemas']['CalendarOut']
type Ev = components['schemas']['EventOut']
type Task = components['schemas']['TaskOut']

const WEEK = ['mon', 'tue', 'wed', 'thu', 'fri']

export function EventsRoute() {
  const { t } = useTranslation()
  const session = useSession()
  const activeId = session?.workspaceId
  const [cals, setCals] = useState<Calendar[]>([])
  const [events, setEvents] = useState<Ev[]>([])
  const [dueTasks, setDueTasks] = useState<Task[]>([])
  const [calName, setCalName] = useState('')
  const [holCal, setHolCal] = useState('')
  const [holDay, setHolDay] = useState('')
  const [evTitle, setEvTitle] = useState('')
  const [evStart, setEvStart] = useState('')
  const [evEnd, setEvEnd] = useState('')
  const [err, setErr] = useState<string | null>(null)

  const reload = useCallback(async () => {
    const h = workspaceHeader()
    const [c, e, tk] = await Promise.all([
      api.GET('/calendars', { params: { header: h } }),
      api.GET('/events', { params: { header: h } }),
      api.GET('/tasks', { params: { header: h } }),
    ])
    if (c.data) setCals(c.data)
    if (e.data) setEvents(e.data)
    if (tk.data) setDueTasks(tk.data.filter((x) => x.due_date != null))
  }, [])

  useEffect(() => {
    let active = true
    void (async () => {
      const h = workspaceHeader()
      const [c, e, tk] = await Promise.all([
        api.GET('/calendars', { params: { header: h } }),
        api.GET('/events', { params: { header: h } }),
        api.GET('/tasks', { params: { header: h } }),
      ])
      if (!active) return
      if (c.data) setCals(c.data)
      if (e.data) setEvents(e.data)
      if (tk.data) setDueTasks(tk.data.filter((x) => x.due_date != null))
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

  async function onAddEvent(e: FormEvent) {
    e.preventDefault()
    setErr(null)
    const { error } = await api.POST('/events', {
      params: { header: workspaceHeader() },
      body: { title: evTitle, start_at: evStart, end_at: evEnd },
    })
    if (error) {
      // Overlap for the same person -> event.overlap (no ubiquity).
      setErr(errMessage(error))
      return
    }
    setEvTitle('')
    await reload()
  }

  async function onDelete(id: string) {
    setErr(null)
    const { error } = await api.DELETE('/events/{event_id}', {
      params: { header: workspaceHeader(), path: { event_id: id } },
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
      {events.length === 0 ? (
        <p className="hint">{t('events.none')}</p>
      ) : (
        <ul className="list">
          {events.map((ev) => (
            <li key={ev.id}>
              {ev.title}{' '}
              <span className="muted">
                {ev.start_at} {'->'} {ev.end_at}
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
          ...events.map((ev) => ({
            key: `e${ev.id}`,
            when: ev.start_at,
            label: ev.title,
            to: null as string | null,
          })),
          ...dueTasks.map((tk) => ({
            key: `t${tk.id}`,
            when: `${tk.due_date}T00:00`,
            label: tk.title,
            to: `/tasks/${tk.id}`,
          })),
        ].sort((a, b) => a.when.localeCompare(b.when))
        return items.length === 0 ? (
          <p className="hint">{t('events.none')}</p>
        ) : (
          <ul className="list">
            {items.map((it) => (
              <li key={it.key}>
                <span className="muted">{it.when.slice(0, 16)}</span>{' '}
                {it.to ? (
                  <Link to={it.to}>📋 {it.label}</Link>
                ) : (
                  <>📅 {it.label}</>
                )}
              </li>
            ))}
          </ul>
        )
      })()}
    </section>
  )
}
