import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api, workspaceHeader } from '../api/client'
import type { components } from '../shared'

// ADR-0036 audit panel: the coordinated event stream on this workspace's
// graph -- read/propose/commit/reject/snapshot events, newest first. A
// first-class subscriber of the bus (consumer #3). "Show, never judge":
// the verbatim events, never a verdict. Live push (SSE) is a follow-up;
// this view reads the durable outbox over REST.

type Ev = components['schemas']['GardenEventOut']

const APPLIED = ['committed', 'rejected', 'merged'] as const

function GardenAuditRow({ e, locale }: { e: Ev; locale: string }) {
  const { t } = useTranslation()
  const when = new Date(e.ts).toLocaleString(locale, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
  const payload = (e.payload ?? {}) as { action?: unknown; suggestion_type?: unknown }
  const action = typeof payload.action === 'string' ? payload.action : null
  const subject = typeof payload.suggestion_type === 'string' ? payload.suggestion_type : null
  const applied =
    e.applied_state && (APPLIED as readonly string[]).includes(e.applied_state)
      ? t(`gardenAudit.applied.${e.applied_state}`)
      : null

  return (
    <li className="ghaudit__item">
      <time className="ghaudit__time" dateTime={e.ts}>
        {when}
      </time>
      <span className={`ghaudit__actor ghaudit__actor--${e.actor_kind}`}>
        {t(`gardenAudit.actor.${e.actor_kind}`)}
      </span>
      <span className="ghaudit__kind">{t(`gardenAudit.kind.${e.kind}`)}</span>
      <span className="ghaudit__detail">
        {subject ? <span className="ghaudit__subject">{subject}</span> : null}
        {action ? <span className="ghaudit__action">{action}</span> : null}
        {e.node_id ? <code className="ghaudit__node">{e.node_id.slice(0, 8)}</code> : null}
      </span>
      {applied ? (
        <span className={`ghaudit__applied ghaudit__applied--${e.applied_state}`}>{applied}</span>
      ) : null}
    </li>
  )
}

export function GardenAuditRoute() {
  const { t, i18n } = useTranslation()
  const [events, setEvents] = useState<Ev[] | null | undefined>(undefined)

  useEffect(() => {
    let active = true
    void api
      .GET('/garden/audit', { params: { header: workspaceHeader(), query: { days: 90 } } })
      .then((r) => {
        if (active) setEvents(r.data ?? null)
      })
    return () => {
      active = false
    }
  }, [])

  return (
    <div className="ghaudit">
      <header className="ghaudit__head">
        <h1>{t('gardenAudit.title')}</h1>
        <Link to="/garden" className="ghaudit__back">
          {t('gardenAudit.back')}
        </Link>
      </header>
      <p className="ghaudit__intro">{t('gardenAudit.intro')}</p>

      {events === undefined && <p className="hint">{t('common.loading')}</p>}
      {events === null && <p className="error">{t('gardenAudit.loadError')}</p>}
      {events && events.length === 0 && (
        <p className="ghaudit__empty">{t('gardenAudit.empty')}</p>
      )}
      {events && events.length > 0 && (
        <ul className="ghaudit__list">
          {events.map((e) => (
            <GardenAuditRow key={e.id} e={e} locale={i18n.language} />
          ))}
        </ul>
      )}
    </div>
  )
}
