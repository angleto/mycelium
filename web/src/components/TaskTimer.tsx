import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import { hms, elapsedSec } from '../lib/time'
import type { components } from '../api/schema'

type Entry = components['schemas']['TimeEntryOut']

// Start/stop the timer for ONE task, with a live elapsed readout.
// Reused wherever you work "on a task" outside the time view — e.g.
// inside a task's work note — so the time is still billed to the task
// (task → project → client). Reuses the existing /time endpoints; no
// new billing model.
export function TaskTimer({ taskId }: { taskId: string }) {
  const { t } = useTranslation()
  const [entry, setEntry] = useState<Entry | null>(null)
  const [now, setNow] = useState(() => Date.now())
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    const tick = async () => {
      const { data } = await api.GET('/time/running', {
        params: { header: workspaceHeader() },
      })
      if (active) {
        setEntry((data ?? []).find((r) => r.task_id === taskId) ?? null)
      }
    }
    void tick()
    const poll = setInterval(() => void tick(), 5000)
    return () => {
      active = false
      clearInterval(poll)
    }
  }, [taskId])

  useEffect(() => {
    const h = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(h)
  }, [])

  async function start() {
    setBusy(true)
    setErr(null)
    const { error } = await api.POST('/time/start', {
      params: { header: workspaceHeader() },
      body: { task_id: taskId, parallel: false },
    })
    setBusy(false)
    if (error) {
      setErr(errMessage(error))
      return
    }
    const { data } = await api.GET('/time/running', {
      params: { header: workspaceHeader() },
    })
    setEntry((data ?? []).find((r) => r.task_id === taskId) ?? null)
  }

  async function stop() {
    setBusy(true)
    setErr(null)
    const { error } = await api.POST('/time/stop', {
      params: { header: workspaceHeader() },
      body: { task_id: taskId },
    })
    setBusy(false)
    if (error) {
      setErr(errMessage(error))
      return
    }
    setEntry(null)
  }

  return (
    <span className="tasktimer">
      {entry ? (
        <>
          <span className="tasktimer__on">
            ⏱ {hms(elapsedSec(entry.started_at, now))}
          </span>
          <button
            type="button"
            className="btn--sm btn--danger"
            disabled={busy}
            onClick={() => void stop()}
          >
            {t('time.stop')}
          </button>
        </>
      ) : (
        <button
          type="button"
          className="btn--sm"
          disabled={busy}
          onClick={() => void start()}
        >
          {t('time.start')}
        </button>
      )}
      {err && <span className="err">{err}</span>}
    </span>
  )
}
