import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import { useSession } from '../auth/useSession'
import {
  digestMarkdown,
  digestWindow,
  eventLine,
  fetchWorkDigest,
  hoursLabel,
  type WorkDigest,
} from '../api/workDigest'
import { PeriodPicker } from '../components/PeriodPicker'
import { useFocus } from '../lib/focus'
import { useLinkedClientProject } from '../lib/linkedClientProject'
import { periodRange, type Period } from '../lib/period'
import type { components } from '../shared'

type Tag = components['schemas']['TagOut']
type Project = components['schemas']['ProjectOut']

// The work diary of a period (ADR-0070): every task of a client or project
// that had something happen to it, with what happened, in order. The page a
// person opens before a status meeting, and the one whose Markdown they
// paste into the notes or into a model that drafts the report.
export function DiaryRoute() {
  const { t } = useTranslation()
  const session = useSession()
  const activeId = session?.workspaceId
  const [clients, setClients] = useState<Tag[]>([])
  const [projects, setProjects] = useState<Tag[]>([])
  const [profiles, setProfiles] = useState<Project[]>([])
  const {
    clientId,
    projectId,
    onPickClient,
    onPickProject,
    setClientId,
    setProjectId,
    filterProjectsByClient,
  } = useLinkedClientProject(profiles)
  // The sidebar's focus is where the diary starts: a person focused on a
  // client opens the diary of that client. The pickers stay this page's
  // own afterwards, so looking at another client here does not move the
  // focus of every other view.
  const focus = useFocus()
  useEffect(() => {
    setClientId(focus.clientId)
    setProjectId(focus.projectId)
  }, [focus.clientId, focus.projectId, setClientId, setProjectId])

  // A status meeting is weekly, so the week is where the page opens.
  const [period, setPeriod] = useState<Period>('week')
  const [anchor, setAnchor] = useState<Date>(() => new Date())
  const [from, setFrom] = useState(() => periodRange('week', new Date()).from)
  const [to, setTo] = useState(() => periodRange('week', new Date()).to)

  const [digest, setDigest] = useState<WorkDigest | null>(null)
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    let active = true
    void (async () => {
      const h = workspaceHeader()
      const [cl, pr, pp] = await Promise.all([
        api.GET('/tags', { params: { header: h, query: { kind: 'client' } } }),
        api.GET('/tags', { params: { header: h, query: { kind: 'project' } } }),
        api.GET('/projects', { params: { header: h } }),
      ])
      if (!active) return
      if (cl.data) setClients(cl.data)
      if (pr.data) setProjects(pr.data)
      if (pp.data) setProfiles(pp.data)
    })()
    return () => {
      active = false
    }
  }, [activeId])

  const range = useMemo(() => digestWindow(from, to), [from, to])

  useEffect(() => {
    if (!range) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setDigest(null)
      return
    }
    let active = true
    setLoading(true)
    setErr(null)
    void (async () => {
      try {
        const d = await fetchWorkDigest({
          ...range,
          ...(clientId ? { client_tag_id: clientId } : {}),
          ...(projectId ? { project_tag_id: projectId } : {}),
        })
        if (active) setDigest(d)
      } catch (e) {
        if (active) {
          setDigest(null)
          setErr(errMessage(e))
        }
      } finally {
        if (active) setLoading(false)
      }
    })()
    return () => {
      active = false
    }
  }, [range, clientId, projectId, activeId])

  function onPeriod(p: Period, a: Date): void {
    setPeriod(p)
    setAnchor(a)
    if (p !== 'custom') {
      const r = periodRange(p, a)
      setFrom(r.from)
      setTo(r.to)
    }
  }

  const scopeLabel =
    projects.find((p) => p.id === projectId)?.name ??
    clients.find((c) => c.id === clientId)?.name ??
    t('diary.allWork')

  async function copyMarkdown(): Promise<void> {
    if (!digest) return
    await navigator.clipboard.writeText(digestMarkdown(t, digest, { scope: scopeLabel, from, to }))
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  return (
    <section className="card card--wide diary">
      <h2>{t('diary.title')}</h2>
      <p className="hint">{t('diary.intro')}</p>
      <div className="row">
        <label>
          {t('diary.client')}
          <select value={clientId} onChange={(e) => onPickClient(e.target.value)}>
            <option value="">{t('diary.anyClient')}</option>
            {clients.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          {t('diary.project')}
          <select value={projectId} onChange={(e) => onPickProject(e.target.value)}>
            <option value="">{t('diary.anyProject')}</option>
            {filterProjectsByClient(projects).map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        </label>
      </div>
      <PeriodPicker
        period={period}
        anchor={anchor}
        onChange={onPeriod}
        periods={['week', 'month', 'custom']}
      />
      {period === 'custom' && (
        <div className="row">
          <label>
            {t('time.from')}
            <input type="date" value={from} onChange={(e) => setFrom(e.target.value)} />
          </label>
          <label>
            {t('time.to')}
            <input type="date" value={to} onChange={(e) => setTo(e.target.value)} />
          </label>
        </div>
      )}

      {err && <p className="err">{err}</p>}
      {!range && <p className="err">{t('diary.badRange')}</p>}
      {loading && !digest && <p className="muted">{t('diary.loading')}</p>}

      {digest && (
        <>
          <div className="diary__summary">
            <span>
              {t('diary.total')}: <strong>{hoursLabel(digest.seconds)}</strong> (
              {t('diary.billable')} {hoursLabel(digest.billable_seconds)}),{' '}
              {t('diary.taskCount', { count: digest.tasks.length })}
            </span>
            <button
              type="button"
              className="btn--sm"
              onClick={() => void copyMarkdown()}
              disabled={digest.tasks.length === 0}
            >
              {copied ? t('diary.copied') : t('diary.copyMarkdown')}
            </button>
          </div>
          {digest.withheld.length > 0 && (
            <p className="hint">
              {t('diary.withheld')}: {digest.withheld.join(', ')}
            </p>
          )}
          {digest.tasks.length === 0 && <p className="muted">{t('diary.empty')}</p>}
          <ol className="diary__tasks">
            {digest.tasks.map((task) => (
              <li key={task.task_id} className="diary__task">
                <div className="diary__taskhead">
                  <Link to={`/tasks/${task.task_id}`}>{task.title}</Link>
                  <span className="muted">
                    {[task.state, task.project_name].filter(Boolean).join(' · ')}
                  </span>
                  <strong className="diary__hours">{hoursLabel(task.seconds)}</strong>
                </div>
                {task.description?.trim() && (
                  <details>
                    <summary>{t('diary.description')}</summary>
                    <div className="diary__text">{task.description}</div>
                  </details>
                )}
                <ul className="diary__events">
                  {task.events.map((e, i) => (
                    <li key={i} className={`diary__event diary__event--${e.kind}`}>
                      <time dateTime={e.at} className="muted">
                        {new Date(e.at).toLocaleString(undefined, {
                          weekday: 'short',
                          day: '2-digit',
                          month: 'short',
                          hour: '2-digit',
                          minute: '2-digit',
                        })}
                      </time>{' '}
                      <span>{eventLine(t, e)}</span>
                      {e.text?.trim() && <div className="diary__text">{e.text}</div>}
                    </li>
                  ))}
                </ul>
              </li>
            ))}
          </ol>
        </>
      )}
    </section>
  )
}
