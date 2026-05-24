import { useCallback, useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { authFetch, workspaceHeader } from '../api/client'

// /actors search result (#21 Stage A) reused for the participant
// picker. ``ref_id`` is user.id (kind=user) or ai_assistant.id
// (kind=ai_assistant); the participants service accepts ``handle``
// so we can avoid the extra round-trip to resolve identity_id.
type Actor = {
  handle: string
  kind: 'user' | 'ai_assistant'
  display_name: string
  ref_id: string
}

type ParticipantOut = {
  identity_id: string
  handle: string
  kind: string
  start_at: string
  duration_minutes: number
}

// Visible only when the parent task is an appointment-task (the parent
// gates rendering on ``task.duration_minutes != null``). The section
// keeps the due-date workflow untouched: plain tasks / reminders never
// see this UI.
//
// The list always contains the assignee (mirrored into
// ``task_participants`` by the 0096 trigger); the picker adds N extra
// identities. Removal is allowed for everyone except the assignee:
// removing the assignee row would just be re-inserted by the trigger
// on the next task update, so we hide the X for that one row.
export function ParticipantsSection({
  taskId,
  assigneeIdentityId,
}: {
  taskId: string
  assigneeIdentityId: string | null
}) {
  const { t } = useTranslation()
  const [rows, setRows] = useState<ParticipantOut[]>([])
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [q, setQ] = useState('')
  const [open, setOpen] = useState(false)
  const [matches, setMatches] = useState<Actor[]>([])
  const blurTimer = useRef<number | null>(null)

  const load = useCallback(async () => {
    setErr(null)
    const r = await authFetch(`/tasks/${taskId}/participants`, {
      headers: workspaceHeader() as Record<string, string>,
    })
    if (r.ok) {
      setRows((await r.json()) as ParticipantOut[])
    } else {
      setErr(`HTTP ${r.status}`)
    }
  }, [taskId])

  useEffect(() => {
    let active = true
    void (async () => {
      const r = await authFetch(`/tasks/${taskId}/participants`, {
        headers: workspaceHeader() as Record<string, string>,
      })
      if (!active) return
      if (r.ok) setRows((await r.json()) as ParticipantOut[])
      else setErr(`HTTP ${r.status}`)
    })()
    return () => {
      active = false
    }
  }, [taskId])

  const search = useCallback(async (needle: string) => {
    setBusy(true)
    const params = new URLSearchParams()
    if (needle) params.set('q', needle)
    params.set('limit', '25')
    const r = await authFetch(`/actors?${params.toString()}`, {
      headers: workspaceHeader() as Record<string, string>,
    })
    if (r.ok) setMatches((await r.json()) as Actor[])
    else setMatches([])
    setBusy(false)
  }, [])

  useEffect(() => {
    if (!open) return
    const h = window.setTimeout(() => void search(q.trim()), 200)
    return () => window.clearTimeout(h)
  }, [q, open, search])

  async function add(a: Actor) {
    setErr(null)
    const r = await authFetch(`/tasks/${taskId}/participants`, {
      method: 'POST',
      headers: {
        ...(workspaceHeader() as Record<string, string>),
        'content-type': 'application/json',
      },
      body: JSON.stringify({ handle: a.handle }),
    })
    if (r.ok) {
      setQ('')
      setOpen(false)
      await load()
    } else {
      const body = (await r.json().catch(() => ({}))) as { code?: string }
      // The server emits event.overlap (409) on no-ubiquity; surface
      // the i18n key directly so the message stays consistent with the
      // rest of the calendar surface.
      setErr(body.code === 'event.overlap' ? t('participants.overlap') : `HTTP ${r.status}`)
    }
  }

  async function remove(identityId: string) {
    setErr(null)
    const r = await authFetch(`/tasks/${taskId}/participants/${identityId}`, {
      method: 'DELETE',
      headers: workspaceHeader() as Record<string, string>,
    })
    if (r.ok) await load()
    else setErr(`HTTP ${r.status}`)
  }

  return (
    <section className="participants">
      <h2>{t('participants.title')}</h2>
      <p className="hint">{t('participants.hint')}</p>
      {err && <p className="err">{err}</p>}
      <ul className="list participants__list">
        {rows.map((p) => {
          const isAssignee = p.identity_id === assigneeIdentityId
          return (
            <li key={p.identity_id} className="participants__row">
              <span className="chip" title={p.handle}>
                <span className="chip__glyph" aria-hidden="true">
                  {p.kind === 'user' ? '▲' : '◆'}
                </span>
                @{p.handle}
                {isAssignee && (
                  <span className="muted" style={{ marginLeft: '0.5em' }}>
                    {t('participants.assignee')}
                  </span>
                )}
              </span>
              {!isAssignee && (
                <button
                  type="button"
                  className="assignpick__clear"
                  title={t('participants.remove')}
                  onClick={() => void remove(p.identity_id)}
                >
                  ✕
                </button>
              )}
            </li>
          )
        })}
        {rows.length === 0 && <li className="hint">{t('participants.none')}</li>}
      </ul>
      <div className="assignpick__searchbox">
        <input
          className="assignpick__search"
          placeholder={t('participants.add')}
          value={q}
          onFocus={() => {
            if (blurTimer.current != null) {
              window.clearTimeout(blurTimer.current)
              blurTimer.current = null
            }
            setOpen(true)
          }}
          onBlur={() => {
            blurTimer.current = window.setTimeout(() => setOpen(false), 150)
          }}
          onChange={(e) => setQ(e.target.value)}
        />
        {open && (
          <ul className="assignpick__list">
            {busy && <li className="hint assignpick__hint">…</li>}
            {!busy && matches.length === 0 && (
              <li className="hint assignpick__hint">
                {t('participants.noMatch')}
              </li>
            )}
            {matches
              .filter((a) => !rows.some((p) => p.handle === a.handle))
              .map((a) => (
                <li key={`${a.kind}-${a.ref_id}`}>
                  <button
                    type="button"
                    className="assignpick__opt"
                    onMouseDown={(e) => e.preventDefault()}
                    onClick={() => void add(a)}
                  >
                    <span className="chip__glyph" aria-hidden="true">
                      {a.kind === 'user' ? '▲' : '◆'}
                    </span>
                    <span className="assignpick__handle">@{a.handle}</span>
                    <span className="muted">{a.display_name}</span>
                  </button>
                </li>
              ))}
          </ul>
        )}
      </div>
    </section>
  )
}
