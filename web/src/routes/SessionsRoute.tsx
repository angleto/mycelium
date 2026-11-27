import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import { copyToClipboard } from '../lib/clipboard'
import { relTime } from '../lib/time'
import {
  messageState,
  sessionMeta,
  sessionName,
  sessionState,
  sessionTone,
  type SessionMessage,
  type SessionRow,
} from '../lib/sessionView'
import { serverNowMs, useSessions } from '../lib/useSessions'

// The sessions a person runs, read top to bottom in the order the
// questions are asked (docs/adr/0078): which one needs me, what the others
// are doing, which have gone quiet. The server groups and orders the rows;
// this page only says what each one is doing, in words, and how long for.
//
// What it replaced: a collapsed list on the tasks board, one row per
// worker, named by a label the agent made up or by eight characters of a
// uuid, with "last seen" and nothing else. It could not say which session
// was waiting, because nothing told it.

export const INSTALL_COMMAND = 'mycelium session install-hooks --apply'

// The person's messages to their sessions (docs/adr/0079), keyed by session.
// Read with every refresh of the sessions, and again after a send: a second
// clock would only disagree with the first.
type Inbox = { bySession: Map<string, SessionMessage[]>; reload: () => void }

function useInbox(wanted: boolean, tick: number): Inbox {
  const [bySession, setBySession] = useState<Map<string, SessionMessage[]>>(new Map())
  const [nudge, setNudge] = useState(0)
  useEffect(() => {
    if (!wanted) return
    let live = true
    void api
      .GET('/sessions/messages', { params: { header: workspaceHeader() } })
      .then(({ data }) => {
        if (!live || data === undefined) return
        const next = new Map<string, SessionMessage[]>()
        for (const m of data.messages) next.set(m.session_id, [...(next.get(m.session_id) ?? []), m])
        setBySession(next)
      })
    return () => {
      live = false
    }
  }, [wanted, tick, nudge])
  const reload = useCallback(() => setNudge((n) => n + 1), [])
  return { bySession, reload }
}

export function SessionsRoute() {
  const { t, i18n } = useTranslation()
  const s = useSessions()
  const now = serverNowMs(s, s.nowMs)
  const rows = s.page?.sessions ?? []
  const byGroup = (g: SessionRow['group']) => rows.filter((r) => r.group === g)
  const needsYou = byGroup('needs_you')
  const working = byGroup('working')
  const quiet = byGroup('quiet')
  const inbox = useInbox(rows.some((r) => r.can_send), s.receivedAtMs)

  return (
    <section className="card sessionspage">
      <header className="sessionspage__head">
        <h1 className="page-title">{t('sessions.title')}</h1>
        <p className="hint">{t('sessions.intro')}</p>
      </header>

      {s.failed && (
        <p className="err" role="status">
          {s.known
            ? t('sessions.staleRead', { when: relTime(new Date(s.receivedAtMs).toISOString(), i18n.language, s.nowMs) })
            : t('sessions.loadError')}
        </p>
      )}

      {!s.known && !s.failed && <p className="muted">{t('sessions.loading')}</p>}

      {s.known && rows.length === 0 && <EmptyState />}

      {s.known && rows.length > 0 && (
        <>
          <Group
            id="needs-you"
            title={t('sessions.group.needsYou', { count: needsYou.length })}
            empty={t('sessions.group.needsYouEmpty')}
            rows={needsYou}
            now={now}
            inbox={inbox}
          />
          {working.length > 0 && (
            <Group
              id="working"
              title={t('sessions.group.working', { count: working.length })}
              rows={working}
              now={now}
              inbox={inbox}
            />
          )}
          {quiet.length > 0 && (
            <details className="sessionspage__quiet">
              <summary>{t('sessions.group.quiet', { count: quiet.length })}</summary>
              <p className="hint">
                {t('sessions.group.quietHint', {
                  min: Math.round((s.page?.quiet_after_seconds ?? 900) / 60),
                })}
              </p>
              <SessionList rows={quiet} now={now} inbox={inbox} />
            </details>
          )}
        </>
      )}

      {s.known && <p className="hint sessionspage__foot">{t('sessions.refreshNote')}</p>}
    </section>
  )
}

function Group({
  id,
  title,
  empty,
  rows,
  now,
  inbox,
}: {
  id: string
  title: string
  empty?: string
  rows: SessionRow[]
  now: number
  inbox: Inbox
}) {
  const headingId = `sessions-${id}`
  return (
    <section className="sessionspage__group" aria-labelledby={headingId}>
      <h2 id={headingId} className="sessionspage__grouptitle">
        {title}
      </h2>
      {rows.length === 0 ? (
        <p className="muted">{empty}</p>
      ) : (
        <SessionList rows={rows} now={now} inbox={inbox} />
      )}
    </section>
  )
}

function SessionList({ rows, now, inbox }: { rows: SessionRow[]; now: number; inbox: Inbox }) {
  return (
    <ul className="sessionlist">
      {rows.map((r) => (
        <SessionItem key={r.session_id ?? r.worker_ids.join(',')} row={r} now={now} inbox={inbox} />
      ))}
    </ul>
  )
}

function SessionItem({ row, now, inbox }: { row: SessionRow; now: number; inbox: Inbox }) {
  const { t } = useTranslation()
  const tone = sessionTone(row)
  const name = sessionName(row, t)
  return (
    <li className={`sessionrow sessionrow--${tone}`}>
      <span className="sessionrow__mark" aria-hidden="true" />
      <div className="sessionrow__body">
        <div className="sessionrow__line">
          <span className="sessionrow__name">{name}</span>
          <span className="sessionrow__state">{sessionState(row, now, t)}</span>
        </div>
        {row.held.length > 0 ? (
          <ul className="sessionrow__tasks" aria-label={t('sessions.holds', { name })}>
            {row.held.map((h) => (
              <li key={h.task_id}>
                <Link to={`/tasks/${h.task_id}`} className="sessionrow__task" title={h.title}>
                  {h.title}
                </Link>
              </li>
            ))}
          </ul>
        ) : (
          <div className="sessionrow__notask muted">{t('sessions.noTask')}</div>
        )}
        <div className="sessionrow__meta muted">{sessionMeta(row, now, t).join(' · ')}</div>
        {row.can_send && row.session_id && (
          <Composer
            sessionId={row.session_id}
            name={name}
            blocked={row.attention === 'blocked' && row.blocked_on === 'permission'}
            messages={inbox.bySession.get(row.session_id) ?? []}
            onSent={inbox.reload}
          />
        )}
      </div>
    </li>
  )
}

// One line to write to the session, and what became of the last few sent.
// The answer is in the terminal and the transcript; nothing here copies it.
function Composer({
  sessionId,
  name,
  blocked,
  messages,
  onSent,
}: {
  sessionId: string
  name: string
  blocked: boolean
  messages: SessionMessage[]
  onSent: () => void
}) {
  const { t, i18n } = useTranslation()
  const [body, setBody] = useState('')
  const [sending, setSending] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  async function send(e: FormEvent) {
    e.preventDefault()
    if (!body.trim() || sending) return
    setSending(true)
    setErr(null)
    const { error } = await api.POST('/sessions/send', {
      params: { header: workspaceHeader() },
      body: { session_id: sessionId, body },
    })
    setSending(false)
    if (error) {
      setErr(errMessage(error))
      return
    }
    setBody('')
    onSent()
  }

  return (
    <div className="sessioncompose">
      <form className="sessioncompose__form" onSubmit={(e) => void send(e)}>
        <input
          type="text"
          className="sessioncompose__input"
          aria-label={t('sessions.compose.label', { name })}
          placeholder={t('sessions.compose.placeholder')}
          value={body}
          maxLength={8000}
          onChange={(e) => setBody(e.target.value)}
        />
        <button type="submit" className="btn" disabled={sending || !body.trim()}>
          {sending ? t('sessions.compose.sending') : t('sessions.compose.send')}
        </button>
      </form>
      {blocked && <p className="hint">{t('sessions.compose.blocked')}</p>}
      {err && (
        <p className="err" role="alert">
          {err}
        </p>
      )}
      {messages.length > 0 && (
        <ul className="sessioncompose__mine" aria-label={t('sessions.compose.mine', { name })}>
          {messages.map((m) => (
            <li key={m.id} className={`sessionmsg sessionmsg--${m.status}`}>
              <span className="sessionmsg__body">{m.body}</span>
              <span className="sessionmsg__state">{messageState(m, t, i18n.language)}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function EmptyState() {
  const { t } = useTranslation()
  const [copied, setCopied] = useState(false)
  return (
    <div className="sessionspage__empty">
      <p>{t('sessions.empty.lead')}</p>
      <p className="hint">{t('sessions.empty.how')}</p>
      <div className="sessionspage__cmd">
        <code>{INSTALL_COMMAND}</code>
        <button
          type="button"
          className="btn btn--ghost"
          onClick={() =>
            void copyToClipboard(INSTALL_COMMAND, t('sessions.empty.copyPrompt')).then(setCopied)
          }
        >
          {copied ? t('sessions.empty.copied') : t('sessions.empty.copy')}
        </button>
      </div>
    </div>
  )
}
