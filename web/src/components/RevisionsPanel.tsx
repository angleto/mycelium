import { useCallback, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import type { components } from '../api/schema'

type Revision = components['schemas']['RevisionOut']

interface RevisionsPanelProps {
  kind: 'task' | 'note'
  id: string
  /** Latest known version; sent as ``expected_version`` on
   * restore. Caller bumps it after a successful restore (the
   * service returns the new version). */
  version: number
  /** Fired after a successful restore. The caller typically
   * refetches the entity to pick up the reverted fields. */
  onRestored?: (newVersion: number) => void
}

function formatTime(iso: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString()
}

/** Recovery-history timeline for a single task or note. Shows the
 * latest 50 revisions, most recent first; the row marked
 * ``editing`` corresponds to the open web revision (sealed_at is
 * null on the server). ``Restore`` reverts the entity to the
 * snapshot stored on that row.
 *
 * Diff side-by-side is intentionally NOT here yet — the timeline
 * + restore covers the original "I deleted something by mistake"
 * recovery path. A diff view can land on top of this without
 * changing the data model.
 */
export function RevisionsPanel({
  kind,
  id,
  version,
  onRestored,
}: RevisionsPanelProps) {
  const { t } = useTranslation()
  const [rows, setRows] = useState<Revision[]>([])
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setErr(null)
    if (kind === 'task') {
      const { data, error } = await api.GET('/tasks/{task_id}/revisions', {
        params: { header: workspaceHeader(), path: { task_id: id } },
      })
      setLoading(false)
      if (error) {
        setErr(errMessage(error))
        return
      }
      setRows(data ?? [])
    } else {
      const { data, error } = await api.GET('/notes/{note_id}/revisions', {
        params: { header: workspaceHeader(), path: { note_id: id } },
      })
      setLoading(false)
      if (error) {
        setErr(errMessage(error))
        return
      }
      setRows(data ?? [])
    }
  }, [kind, id])

  // Initial load is part of the panel's lifecycle (subscribe to
  // external timeline as the entity comes into view). The
  // ``active`` flag guards against a race when the user switches
  // entities before the fetch resolves.
  useEffect(() => {
    let active = true
    void (async () => {
      if (kind === 'task') {
        const { data, error } = await api.GET('/tasks/{task_id}/revisions', {
          params: { header: workspaceHeader(), path: { task_id: id } },
        })
        if (!active) return
        if (error) {
          setErr(errMessage(error))
          return
        }
        setRows(data ?? [])
      } else {
        const { data, error } = await api.GET('/notes/{note_id}/revisions', {
          params: { header: workspaceHeader(), path: { note_id: id } },
        })
        if (!active) return
        if (error) {
          setErr(errMessage(error))
          return
        }
        setRows(data ?? [])
      }
    })()
    return () => {
      active = false
    }
  }, [kind, id])

  const restore = useCallback(
    async (rev: Revision) => {
      if (!window.confirm(t('revisions.confirmRestore'))) return
      setBusy(rev.id)
      setErr(null)
      const opts = {
        params: { header: workspaceHeader() },
        body: { expected_version: version },
      }
      if (kind === 'task') {
        const { data, error, response } = await api.POST(
          '/tasks/{task_id}/revisions/{rev_id}/restore',
          {
            params: {
              header: workspaceHeader(),
              path: { task_id: id, rev_id: rev.id },
            },
            body: opts.body,
          },
        )
        setBusy(null)
        if (response.status === 409) {
          setErr(t('tasks.conflict'))
          return
        }
        if (error || !data) {
          setErr(errMessage(error))
          return
        }
        onRestored?.(data.version)
      } else {
        const { data, error, response } = await api.POST(
          '/notes/{note_id}/revisions/{rev_id}/restore',
          {
            params: {
              header: workspaceHeader(),
              path: { note_id: id, rev_id: rev.id },
            },
            body: opts.body,
          },
        )
        setBusy(null)
        if (response.status === 409) {
          setErr(t('tasks.conflict'))
          return
        }
        if (error || !data) {
          setErr(errMessage(error))
          return
        }
        onRestored?.(data.version)
      }
      await load()
    },
    [kind, id, version, onRestored, t, load],
  )

  if (loading && rows.length === 0) {
    return <p className="muted">{t('revisions.loading')}</p>
  }

  return (
    <div className="revisions-panel">
      <h3>{t('revisions.title')}</h3>
      {err && <p className="error">{err}</p>}
      {rows.length === 0 && !loading ? (
        <p className="muted">{t('revisions.empty')}</p>
      ) : (
        <ul className="revisions-list">
          {rows.map((rev) => {
            const open = rev.sealed_at === null
            const ts = open ? rev.last_edit_at : rev.sealed_at
            const channelLabel = t(`revisions.channel.${rev.channel}`, {
              defaultValue: rev.channel,
            })
            const fields = rev.changed_fields.join(', ')
            return (
              <li
                key={rev.id}
                className={`revision-row${open ? ' revision-open' : ''}`}
              >
                <div className="revision-meta">
                  <span className="revision-time">{formatTime(ts)}</span>
                  <span className="revision-channel">{channelLabel}</span>
                  {open && (
                    <span className="revision-open-badge">
                      {t('revisions.editing')}
                    </span>
                  )}
                  {rev.restored_from && (
                    <span className="revision-restored-badge">
                      {t('revisions.restoredFrom')}
                    </span>
                  )}
                </div>
                <div className="revision-fields">{fields}</div>
                {!open && (
                  <button
                    type="button"
                    className="revision-restore"
                    onClick={() => void restore(rev)}
                    disabled={busy === rev.id}
                  >
                    {busy === rev.id
                      ? t('revisions.restoring')
                      : t('revisions.restore')}
                  </button>
                )}
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}
