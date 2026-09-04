import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import { saveWorkspaceSettings, useMyWorkspace } from '../auth/useMyWorkspace'
import type { components } from '../shared'

type Req = components['schemas']['DispatchRequestOut']
type Tick = components['schemas']['DispatchTickOut']
type Policy = components['schemas']['SchedulePolicy']
type Auto = components['schemas']['AutonomousDispatch']

const BAD = new Set(['denied', 'failed'])
// Only an active request can still be decided (backend rejects the rest).
const CAN_APPROVE = new Set(['pending'])
const CAN_DENY = new Set(['pending', 'approved'])

// ADR-0025 P5 — the closed-loop control surface on /schedule: the
// workspace autonomous-dispatch mode, a "run a tick now" button with
// its last-tick summary, and the approval queue. Mode/approve/deny/
// tick are owner-gated server-side; like AgentRunPanel the controls
// are shown and a denial is surfaced (the server is the single
// role-truth source). `policy` is the scheduling policy the tick runs
// under (shared with the recompute selector above).
export function DispatchPanel({
  policy,
  onChanged,
}: {
  policy: Policy
  onChanged?: () => void
}) {
  const { t } = useTranslation()
  const { ws } = useMyWorkspace()
  const [reqs, setReqs] = useState<Req[]>([])
  // The stored policy is the truth; `pending` only holds the value the
  // user just picked, so the select does not snap back while the write
  // is in flight. The workspace version and the estimate presets the
  // PATCH has to restate are the shared snapshot's business now, not
  // this panel's (a private copy of `version` was what made a save here
  // 409 after any save on the settings page).
  const [pending, setPending] = useState<Auto | null>(null)
  const [last, setLast] = useState<Tick | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [tick, setTick] = useState(0)

  useEffect(() => {
    let active = true
    void (async () => {
      const h = workspaceHeader()
      const rq = await api.GET('/dispatch/requests', { params: { header: h } })
      if (!active) return
      if (rq.error) {
        setErr(errMessage(rq.error))
        return
      }
      setReqs(rq.data ?? [])
    })()
    return () => {
      active = false
    }
  }, [tick])

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
    onChanged?.()
  }

  const h = () => workspaceHeader()

  // What the select shows: the value being written if there is one,
  // otherwise the workspace's stored policy.
  const mode: Auto = pending ?? ws?.settings?.autonomous_dispatch ?? 'approval_required'

  function changeMode(v: Auto) {
    setPending(v)
    setBusy(true)
    setErr(null)
    void saveWorkspaceSettings({ autonomous_dispatch: v }).then((res) => {
      setBusy(false)
      setPending(null)
      if (!res.ok) {
        setErr(res.message)
        return
      }
      setTick((n) => n + 1)
      onChanged?.()
    })
  }

  const approve = (r: Req) =>
    run(() =>
      api.POST('/dispatch/requests/{request_id}/approve', {
        params: { header: h(), path: { request_id: r.id } },
        body: { expected_version: r.version },
      }),
    )
  const deny = (r: Req) =>
    run(() =>
      api.POST('/dispatch/requests/{request_id}/deny', {
        params: { header: h(), path: { request_id: r.id } },
        body: { expected_version: r.version },
      }),
    )

  async function doTick() {
    setBusy(true)
    setErr(null)
    const { data, error } = await api.POST('/dispatch/tick', {
      params: { header: h() },
      body: { policy },
    })
    setBusy(false)
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    setLast(data)
    setTick((n) => n + 1)
    onChanged?.()
  }

  return (
    <div className="atts">
      <div className="atts__head">
        <span className="atts__lbl">{t('dispatch.title')}</span>
        <label>
          {t('dispatch.mode')}
          <select
            value={mode}
            disabled={busy}
            onChange={(e) => changeMode(e.target.value as Auto)}
          >
            <option value="off">{t('dispatch.modeOff')}</option>
            <option value="approval_required">
              {t('dispatch.modeApproval')}
            </option>
            <option value="auto">{t('dispatch.modeAuto')}</option>
          </select>
        </label>
        <button
          type="button"
          className="btn--sm"
          disabled={busy || mode === 'off'}
          onClick={() => void doTick()}
        >
          {busy ? t('dispatch.ticking') : t('dispatch.runNow')}
        </button>
      </div>
      <p className="hint">{t('dispatch.intro')}</p>
      {mode === 'off' && <p className="hint">{t('dispatch.disabled')}</p>}
      {last && (
        <p className="hint">
          {t('dispatch.lastTick', {
            created: last.created,
            approved: last.approved,
            dispatched: last.dispatched,
            skipped: last.skipped,
            failed: last.failed,
            makespan: last.projected_makespan_minutes,
            cost: last.projected_credit_cost,
          })}
        </p>
      )}
      {err && <p className="err">{err}</p>}
      {reqs.length === 0 ? (
        <p className="hint">{t('dispatch.queueEmpty')}</p>
      ) : (
        <table className="tbl">
          <thead>
            <tr>
              <th>{t('dispatch.colTask')}</th>
              <th>{t('dispatch.colExecutor')}</th>
              <th>{t('dispatch.colCost')}</th>
              <th>{t('dispatch.colStatus')}</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {reqs.map((r) => (
              <tr key={r.id}>
                <td>
                  <Link to={`/tasks/${r.task_id}`}>
                    {r.task_title || r.task_id.slice(0, 8)}
                  </Link>
                </td>
                <td>{r.executor_name ?? '—'}</td>
                <td>
                  {Number(r.projected_credit_cost) > 0
                    ? r.projected_credit_cost
                    : '—'}
                </td>
                <td>
                  <span
                    className={
                      'tag ' +
                      (BAD.has(r.status) ? 'tag--danger' : 'tag--muted')
                    }
                  >
                    {t(`dispatch.status.${r.status}`)}
                  </span>
                </td>
                <td>
                  {CAN_APPROVE.has(r.status) && (
                    <button
                      type="button"
                      className="btn--sm"
                      disabled={busy}
                      onClick={() => void approve(r)}
                    >
                      {t('dispatch.approve')}
                    </button>
                  )}
                  {CAN_DENY.has(r.status) && (
                    <button
                      type="button"
                      className="btn--sm btn--ghost"
                      disabled={busy}
                      onClick={() => void deny(r)}
                    >
                      {t('dispatch.deny')}
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
