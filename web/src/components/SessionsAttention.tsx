import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { useSessions } from '../lib/useSessions'

// The one thing worth interrupting a person for, on every page: how many of
// their sessions are waiting for them. Nothing at all when none is, so the
// top bar does not carry a zero that has to be read to be dismissed. Marked
// blocked when one of them cannot go on without an answer, which is the
// case where waiting costs the most.
export function SessionsAttention() {
  const { t } = useTranslation()
  const s = useSessions()
  if (!s.known || !s.page) return null
  const n = s.page.counts.needs_you
  if (n === 0) return null
  const blocked = s.page.sessions.some(
    (r) => r.group === 'needs_you' && r.attention === 'blocked',
  )
  return (
    <Link
      to="/sessions"
      className={blocked ? 'sessionsbadge sessionsbadge--blocked' : 'sessionsbadge'}
      aria-label={t('sessions.badge.label', { count: n })}
      title={t('sessions.badge.label', { count: n })}
    >
      <span className="sessionsbadge__dot" aria-hidden="true" />
      <span aria-hidden="true">{t('sessions.badge.text', { count: n })}</span>
    </Link>
  )
}

// The line the tasks board keeps where the collapsed list of workers was:
// how many sessions there are and how many need the person, and the way to
// the page that says which.
export function SessionsSummary() {
  const { t } = useTranslation()
  const s = useSessions()
  if (!s.known || !s.page) return null
  const total = s.page.sessions.length
  if (total === 0) return null
  const n = s.page.counts.needs_you
  return (
    <p className="sessionssummary">
      <Link to="/sessions">
        {t('sessions.summary.total', { count: total })}
        {' · '}
        {n > 0
          ? t('sessions.badge.text', { count: n })
          : t('sessions.summary.noneWaiting')}
        {' →'}
      </Link>
    </p>
  )
}
