import { useCallback, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import type { TFunction } from 'i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import type { components } from '../api/schema'
import { display, equal, fieldType } from '../lib/revisionDiff'

type Revision = components['schemas']['RevisionOut']

interface RevisionsPanelProps {
  kind: 'task' | 'note'
  id: string
  /** Latest known version; sent as ``expected_version`` on
   * restore. Caller bumps it after a successful restore (the
   * service returns the new version). */
  version: number
  /** Current entity state, by field name. Drives the diff
   * side-by-side view: the snapshot of the selected revision is
   * compared against this map. Optional — without it the panel
   * still renders the timeline and supports restore-totale. */
  current?: Record<string, unknown>
  /** Fired after a successful restore. The caller typically
   * refetches the entity to pick up the reverted fields. */
  onRestored?: (newVersion: number) => void
}

/** Fields that show up in the diff side-by-side view for each kind.
 * The list is the union of "fields the server allows to restore"
 * (`restorable_payload` whitelist on the backend) and the practical
 * core that the SPA already exposes in its editors. Per-field
 * restore on a non-listed field is still possible at the API level
 * (`fields: [...]`) but the UI sticks to the curated set. */
const VIEWABLE_FIELDS: Record<'task' | 'note', readonly string[]> = {
  task: [
    'title',
    'description',
    'importance',
    'urgency',
    'start_date',
    'due_date',
    'billable',
    'estimate_effort_h',
    'monetary_cost',
    'location',
    'necessity',
    'start_at',
    'duration_minutes',
  ],
  note: ['title', 'transcript'],
}

function formatTime(iso: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString()
}

/** Recovery-history timeline for a single task or note.
 *
 * - Lists the latest 50 revisions, most recent first; the row with
 *   ``sealed_at === null`` is the open web session, badged
 *   ``editing``.
 * - Clicking a sealed row opens an inline diff side-by-side with
 *   the current entity state. Each row that differs offers a
 *   per-field ``Restore`` button; ``Restore all`` at the bottom
 *   reverts every restorable field.
 * - The set of viewable fields is curated per kind (see
 *   ``VIEWABLE_FIELDS``). The server's restorable-fields whitelist
 *   is authoritative; this list only governs what the UI surfaces.
 */
export function RevisionsPanel({
  kind,
  id,
  version,
  current,
  onRestored,
}: RevisionsPanelProps) {
  const { t } = useTranslation()
  const [rows, setRows] = useState<Revision[]>([])
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)

  const reload = useCallback(async () => {
    setErr(null)
    if (kind === 'task') {
      const { data, error } = await api.GET('/tasks/{task_id}/revisions', {
        params: { header: workspaceHeader(), path: { task_id: id } },
      })
      if (error) {
        setErr(errMessage(error))
        return
      }
      setRows(data ?? [])
    } else {
      const { data, error } = await api.GET('/notes/{note_id}/revisions', {
        params: { header: workspaceHeader(), path: { note_id: id } },
      })
      if (error) {
        setErr(errMessage(error))
        return
      }
      setRows(data ?? [])
    }
  }, [kind, id])

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

  const runRestore = useCallback(
    async (rev: Revision, fields: string[] | null) => {
      const confirmKey = fields
        ? 'revisions.confirmRestoreField'
        : 'revisions.confirmRestore'
      if (!window.confirm(t(confirmKey))) return
      setBusy(rev.id)
      setErr(null)
      const body = fields
        ? { expected_version: version, fields }
        : { expected_version: version }
      if (kind === 'task') {
        const { data, error, response } = await api.POST(
          '/tasks/{task_id}/revisions/{rev_id}/restore',
          {
            params: {
              header: workspaceHeader(),
              path: { task_id: id, rev_id: rev.id },
            },
            body,
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
            body,
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
      await reload()
    },
    [kind, id, version, onRestored, t, reload],
  )

  const restoreAll = useCallback(
    (rev: Revision) => runRestore(rev, null),
    [runRestore],
  )

  const restoreField = useCallback(
    (rev: Revision, field: string) => runRestore(rev, [field]),
    [runRestore],
  )

  return (
    <div className="revisions-panel">
      <h3>{t('revisions.title')}</h3>
      {err && <p className="error">{err}</p>}
      {rows.length === 0 ? (
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
            const isSelected = rev.id === selectedId
            return (
              <li
                key={rev.id}
                className={[
                  'revision-row',
                  open ? 'revision-open' : '',
                  isSelected ? 'revision-selected' : '',
                ]
                  .filter(Boolean)
                  .join(' ')}
              >
                <button
                  type="button"
                  className="revision-meta-btn"
                  onClick={() =>
                    setSelectedId((cur) => (cur === rev.id ? null : rev.id))
                  }
                  disabled={open}
                  aria-expanded={isSelected}
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
                </button>
                {isSelected && !open && (
                  <RevisionDiffView
                    rev={rev}
                    kind={kind}
                    current={current ?? {}}
                    busy={busy === rev.id}
                    onRestoreField={(field) => void restoreField(rev, field)}
                    onRestoreAll={() => void restoreAll(rev)}
                  />
                )}
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}

interface RevisionDiffViewProps {
  rev: Revision
  kind: 'task' | 'note'
  current: Record<string, unknown>
  busy: boolean
  onRestoreField: (field: string) => void
  onRestoreAll: () => void
}

/** Side-by-side diff of a revision's snapshot vs the entity's
 * current state. Rendered when a sealed timeline row is expanded.
 *
 * Equality and rendering go through ``revisionDiff.normalize`` /
 * ``display`` so a Decimal serialised as ``"12.50"`` matches the
 * ``12.5`` number the SPA carries, and an ISO datetime matches a
 * ``Date`` object pointing at the same instant. Without this, the
 * diff would surface false-positive "changes" on every Decimal /
 * date / boolean field. */
function RevisionDiffView({
  rev,
  kind,
  current,
  busy,
  onRestoreField,
  onRestoreAll,
}: RevisionDiffViewProps) {
  const { t } = useTranslation()
  const snap = rev.snapshot as Record<string, unknown>
  const viewable = VIEWABLE_FIELDS[kind]
  const fields = viewable.filter((f) => f in snap || f in current)
  const diffs = fields.filter(
    (f) => !equal(snap[f], current[f], fieldType(kind, f)),
  )

  return (
    <div className="revision-diff">
      {diffs.length === 0 ? (
        <p className="muted">{t('revisions.noDiff')}</p>
      ) : (
        <table className="revision-diff-table">
          <thead>
            <tr>
              <th>{t('revisions.diff.field')}</th>
              <th>{t('revisions.diff.snapshot')}</th>
              <th>{t('revisions.diff.current')}</th>
              <th aria-label="actions" />
            </tr>
          </thead>
          <tbody>
            {diffs.map((f) => {
              const type = fieldType(kind, f)
              return (
                <tr key={f}>
                  <th scope="row">
                    {t(`revisions.diff.fields.${f}`, { defaultValue: f })}
                  </th>
                  <td className="revision-diff-snap">
                    {display(snap[f], type, t as TFunction)}
                  </td>
                  <td className="revision-diff-curr">
                    {display(current[f], type, t as TFunction)}
                  </td>
                  <td>
                    <button
                      type="button"
                      className="revision-restore-field"
                      onClick={() => onRestoreField(f)}
                      disabled={busy}
                    >
                      {t('revisions.restoreField')}
                    </button>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
      <div className="revision-diff-footer">
        <button
          type="button"
          className="revision-restore"
          onClick={onRestoreAll}
          disabled={busy}
        >
          {busy ? t('revisions.restoring') : t('revisions.restoreAll')}
        </button>
      </div>
    </div>
  )
}
