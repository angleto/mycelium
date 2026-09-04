import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import { fmtDateTime } from '../lib/tz'
import type { components } from '../shared'

type Run = components['schemas']['AgentRunOut']

const ACTIVE = new Set(['queued', 'running'])
const BAD = new Set(['failed', 'blocked', 'cancelled'])

// ADR-0025 P3 — on-demand agent execution for an llm_agent task.
// Start spends credits and is owner-gated server-side (a denial is
// surfaced). Polls while a run is active; links to the produced
// work-note artifact.
export function AgentRunPanel({ taskId }: { taskId: string }) {
  const { t } = useTranslation()
  const [runs, setRuns] = useState<Run[]>([])
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [tick, setTick] = useState(0)

  useEffect(() => {
    let active = true
    void (async () => {
      const { data, error } = await api.GET('/agent-runs', {
        params: { header: workspaceHeader(), query: { task_id: taskId } },
      })
      if (!active) return
      if (error) {
        setErr(errMessage(error))
        return
      }
      const sorted = [...(data ?? [])].sort((a, b) =>
        (b.started_at ?? '').localeCompare(a.started_at ?? ''),
      )
      setRuns(sorted)
    })()
    return () => {
      active = false
    }
  }, [taskId, tick])

  const latest = runs[0]
  const live = !!latest && ACTIVE.has(latest.status)

  // Reflect progress: re-poll while a run is active.
  useEffect(() => {
    if (!live) return
    const h = window.setInterval(() => setTick((n) => n + 1), 3000)
    return () => clearInterval(h)
  }, [live])

  async function run() {
    setBusy(true)
    setErr(null)
    const { error } = await api.POST('/tasks/{task_id}/run', {
      params: { header: workspaceHeader(), path: { task_id: taskId } },
    })
    setBusy(false)
    if (error) {
      setErr(errMessage(error))
      return
    }
    setTick((n) => n + 1)
  }

  async function cancel(id: string) {
    setErr(null)
    const { error } = await api.POST('/agent-runs/{run_id}/cancel', {
      params: { header: workspaceHeader(), path: { run_id: id } },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    setTick((n) => n + 1)
  }

  return (
    <div className="atts">
      <div className="atts__head">
        <span className="atts__lbl">{t('agentrun.title')}</span>
        <button
          type="button"
          className="btn--sm"
          disabled={busy || live}
          onClick={() => void run()}
        >
          {live ? t('agentrun.running') : t('agentrun.run')}
        </button>
        {live && latest && !latest.cancel_requested && (
          <button
            type="button"
            className="btn--sm btn--danger"
            onClick={() => void cancel(latest.id)}
          >
            {t('agentrun.cancel')}
          </button>
        )}
      </div>
      <p className="hint">{t('agentrun.intro')}</p>
      {err && <p className="err">{err}</p>}
      {runs.length === 0 ? (
        <p className="hint">{t('agentrun.none')}</p>
      ) : (
        <ul className="list">
          {runs.slice(0, 8).map((r) => (
            <li key={r.id}>
              <span
                className={
                  'tag ' + (BAD.has(r.status) ? 'tag--danger' : 'tag--muted')
                }
              >
                {t(`agentrun.status.${r.status}`)}
              </span>{' '}
              <span className="muted">
                {t('agentrun.steps')} {r.steps} · {r.credits_spent}{' '}
                {t('agentrun.credits')}
                {r.started_at ? ` · ${fmtDateTime(r.started_at)}` : ''}
                {r.blocked_reason
                  ? ` · ${t(`agentrun.blocked.${r.blocked_reason}`)}`
                  : ''}
                {r.error ? ` · ${r.error}` : ''}
              </span>
              {r.artifact_note_id && (
                <>
                  {' · '}
                  <Link to={`/notes/${r.artifact_note_id}`}>
                    {t('agentrun.artifact')}
                  </Link>
                </>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
