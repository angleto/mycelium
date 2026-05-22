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

  async function start(parallel: boolean) {
    setBusy(true)
    setErr(null)
    const { error } = await api.POST('/time/start', {
      params: { header: workspaceHeader() },
      body: { task_id: taskId, parallel, note_id: noteId ?? null },
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

  // The shared timer control: ⏱▶ start (serial), ⏱▶▶ start parallel,
  // ⏱■ stop with a live readout. Same buttons everywhere (task list,
  // kanban, task detail, work notes) — server-authoritative.
  return (
    <span className="tasktimer">
      {entry ? (
        <button
          type="button"
          className="btn--sm tasktimer__stop"
          disabled={busy}
          title={t('time.stop')}
          aria-label={t('time.stop')}
          onClick={() => void stop()}
        >
          ⏱■ {hms(elapsedSec(entry.started_at, now))}
        </button>
      ) : (
        <>
          <button
            type="button"
            className="btn--ghost btn--sm"
            disabled={busy}
            title={t('time.startSerial')}
            aria-label={t('time.startSerial')}
            onClick={() => void start(false)}
          >
            ⏱▶
          </button>
          <button
            type="button"
            className="btn--ghost btn--sm"
            disabled={busy}
            title={t('time.startParallel')}
            aria-label={t('time.startParallel')}
            onClick={() => void start(true)}
          >
            ⏱▶▶
          </button>
        </>
      )}
      {err && <span className="err">{err}</span>}
    </span>
  )
}
