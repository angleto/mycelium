import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import { refreshRunning } from '../lib/useRunningTimer'
import type { components } from '../api/schema'

type Entry = components['schemas']['TimeEntryOut']

// Pencil control + small popover to set/edit the free-text `memo` on a
// running time entry ("design", "review", "meeting", ...). Sits next to
// the pause/stop controls on the top-bar RunningIndicator and on every
// TaskTimer, so the user can label what the current session is about
// without leaving the page.
//
// Server-authoritative like the rest of the timer subsystem: it PATCHes
// /time/entries/{id} with the optimistic expected_version, then
// reconciles via the shared refreshRunning() so every consumer (the
// chip, every TaskTimer) sees the new memo/version. No optimistic client
// mutation. A stale version (entry stopped or edited elsewhere) surfaces
// the conflict and reseeds for retry.
export function MemoPopover({
  entry,
  triggerClassName = 'btn--ghost btn--sm',
  onOpenChange,
}: {
  entry: Entry
  triggerClassName?: string
  // Lets a host pause behaviour that would swap the entry out while the
  // popover is open (the RunningIndicator auto-advances between timers).
  onOpenChange?: (open: boolean) => void
}) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const wrapRef = useRef<HTMLSpanElement>(null)
  const taRef = useRef<HTMLTextAreaElement>(null)

  function setOpenState(next: boolean) {
    setOpen(next)
    onOpenChange?.(next)
  }

  function toggle() {
    if (!open) {
      // Seed from server truth each time it opens.
      setText(entry.memo ?? '')
      setErr(null)
    }
    setOpenState(!open)
  }

  function close() {
    setOpenState(false)
  }

  // Esc to close, click-outside to close, focus the field. Armed only
  // while open.
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation()
        setOpenState(false)
      }
    }
    const onDown = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) {
        setOpenState(false)
      }
    }
    document.addEventListener('keydown', onKey)
    document.addEventListener('mousedown', onDown)
    taRef.current?.focus()
    return () => {
      document.removeEventListener('keydown', onKey)
      document.removeEventListener('mousedown', onDown)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  async function save() {
    setBusy(true)
    setErr(null)
    const { error } = await api.PATCH('/time/entries/{entry_id}', {
      params: { header: workspaceHeader(), path: { entry_id: entry.id } },
      // exclude_unset on the server means memo must be sent explicitly;
      // empty input clears it (null).
      body: { expected_version: entry.version, memo: text.trim() || null },
    })
    setBusy(false)
    if (error) {
      setErr(errMessage(error))
      await refreshRunning()
      return
    }
    await refreshRunning()
    close()
  }

  return (
    <span
      className="memo-pop-wrap"
      ref={wrapRef}
      style={{ position: 'relative', display: 'inline-flex' }}
    >
      <button
        type="button"
        className={triggerClassName}
        disabled={busy}
        aria-label={t('time.memoEdit')}
        aria-expanded={open}
        title={entry.memo ? entry.memo : t('time.memoEdit')}
        onClick={toggle}
      >
        ✎
      </button>
      {open && (
        <div
          className="anno-pop"
          style={{ position: 'absolute', top: '100%', right: 0, marginTop: '4px' }}
        >
          <textarea
            ref={taRef}
            className="anno-pop__input"
            rows={3}
            value={text}
            placeholder={t('time.memoPlaceholder')}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              // Cmd/Ctrl+Enter saves, matching the other compose boxes.
              if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
                e.preventDefault()
                void save()
              }
            }}
          />
          <div className="anno-pop__actions">
            <button
              type="button"
              className="btn--sm"
              disabled={busy}
              onClick={() => void save()}
            >
              {t('time.save')}
            </button>
            <button
              type="button"
              className="btn--ghost btn--sm"
              disabled={busy}
              onClick={close}
            >
              {t('notes.close')}
            </button>
          </div>
          {err && <p className="err anno-pop__err">{err}</p>}
        </div>
      )}
    </span>
  )
}
