import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import { hms, elapsedSec } from '../lib/time'
import { useRunningTimers, refreshRunning } from '../lib/useRunningTimer'

// Start/stop the timer for ONE task, with a live elapsed readout.
// Reused wherever you work "on a task" outside the time view — e.g.
// inside a task's work note — so the time is still billed to the task
// (task → project → client). When mounted in a note, `noteId` is
// recorded as provenance on the entry (the work was done in that note).
//
// State is server-authoritative: a running timer is a server row;
// elapsed is derived from the server `started_at` via the shared
// useRunningTimers source, never accumulated client-side. Closing the
// lid / disconnecting / reloading cannot drift or lose time, and a
// stop done elsewhere is reflected on resume.
export function TaskTimer({
  taskId,
  noteId,
}: {
  taskId: string
  noteId?: string
}) {
  const { t } = useTranslation()
  const { running, now } = useRunningTimers()
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const entry = running.find((r) => r.task_id === taskId) ?? null

  async function start() {
    setBusy(true)
    setErr(null)
    const { error } = await api.POST('/time/start', {
      params: { header: workspaceHeader() },
      body: { task_id: taskId, parallel: false, note_id: noteId ?? null },
    })
    setBusy(false)
    if (error) {
      setErr(errMessage(error))
      return
    }
    await refreshRunning()
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
    await refreshRunning()
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
