import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import { fmtDateTime } from '../lib/tz'
import type { components } from '../api/schema'

type Handoff = components['schemas']['HandoffOut']

const BAD = new Set(['cancelled'])

// ADR-0025 P4 — coordination view for a task: the handoff envelopes
// along its dependency edges (work received from predecessors =
// incoming, produced for successors = outgoing) plus the contract-net
// controls. Offer is owner-gated server-side; like AgentRunPanel the
// button is shown and a denial is surfaced (the server is the single
// source of role truth). Claim/decline are member actions on an
// offered task.
export function CoordinationPanel({
  taskId,
  offered,
  titleOf,
  onChanged,
}: {
  taskId: string
  offered: boolean
  titleOf: (id: string) => string
  onChanged: () => void
}) {
  const { t } = useTranslation()
  const [items, setItems] = useState<Handoff[]>([])
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [tick, setTick] = useState(0)

  useEffect(() => {
    let active = true
    void (async () => {
      const { data, error } = await api.GET('/tasks/{task_id}/handoffs', {
        params: { header: workspaceHeader(), path: { task_id: taskId } },
      })
      if (!active) return
      if (error) {
        setErr(errMessage(error))
        return
      }
      setItems(data ?? [])
    })()
    return () => {
      active = false
    }
  }, [taskId, tick])

  // openapi-fetch returns {data,error,response}; we only need error.
  async function run(fn: () => Promise<{ error?: unknown }>) {
    setBusy(true)
    setErr(null)
    const { error } = await fn()
    setBusy(false)
    if (error) {
      setErr(errMessage(error))
      return
    }
    setTick((n) => n + 1)
    onChanged()
  }

  const params = {
    params: { header: workspaceHeader(), path: { task_id: taskId } },
  }
  const offer = () =>
    run(() => api.POST('/tasks/{task_id}/offer', params))
  const claim = () =>
    run(() => api.POST('/tasks/{task_id}/claim', params))
  const decline = () =>
    run(() => api.POST('/tasks/{task_id}/decline', params))

  const incoming = items.filter((h) => h.successor_task_id === taskId)
  const outgoing = items.filter((h) => h.predecessor_task_id === taskId)

  // ``otherId`` is the task on the other side of the handoff (the
  // predecessor for an incoming envelope, the successor for an
  // outgoing one). The title becomes a Link so the user can navigate
  // the coordination graph the same way the Related tasks block
  // already supports — reported as task #1 in the v1.2.74 UX pass.
  const row = (h: Handoff, otherId: string) => (
    <li key={h.id}>
      <span
        className={'tag ' + (BAD.has(h.status) ? 'tag--danger' : 'tag--muted')}
      >
        {t(`coord.status.${h.status}`)}
      </span>{' '}
      <Link to={`/tasks/${otherId}`} className="muted">
        {titleOf(otherId)}
      </Link>
      {h.delivered_at && (
        <span className="muted"> · {fmtDateTime(h.delivered_at)}</span>
      )}
      {h.message && <div>{h.message}</div>}
      {h.artifact_note_id && (
        <Link to={`/notes?open=${h.artifact_note_id}`}>
          {t('coord.artifact')}
        </Link>
      )}
    </li>
  )

  return (
    <div className="atts">
      <div className="atts__head">
        <span className="atts__lbl">{t('coord.title')}</span>
        {offered ? (
          <>
            <span className="tag tag--muted">{t('coord.offered')}</span>
            <button
              type="button"
              className="btn--sm"
              disabled={busy}
              onClick={() => void claim()}
            >
              {t('coord.claim')}
            </button>
            <button
              type="button"
              className="btn--sm btn--ghost"
              disabled={busy}
              onClick={() => void decline()}
            >
              {t('coord.decline')}
            </button>
          </>
        ) : (
          <button
            type="button"
            className="btn--sm"
            disabled={busy}
            onClick={() => void offer()}
          >
            {t('coord.offer')}
          </button>
        )}
      </div>
      <p className="hint">{t('coord.intro')}</p>
      {err && <p className="err">{err}</p>}
      <strong>{t('coord.incoming')}</strong>
      {incoming.length === 0 ? (
        <p className="hint">{t('coord.none')}</p>
      ) : (
        <ul className="list">
          {incoming.map((h) => row(h, h.predecessor_task_id))}
        </ul>
      )}
      <strong>{t('coord.outgoing')}</strong>
      {outgoing.length === 0 ? (
        <p className="hint">{t('coord.none')}</p>
      ) : (
        <ul className="list">
          {outgoing.map((h) => row(h, h.successor_task_id))}
        </ul>
      )}
    </div>
  )
}
