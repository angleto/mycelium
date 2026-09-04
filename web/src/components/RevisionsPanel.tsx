import { useCallback, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import type { TFunction } from 'i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import type { components } from '../shared'
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

// ``parts[N].<what>``: a body field, or a structural lifecycle tag
// (``_create`` / ``_trash`` / ``_restore`` / ``_purge``). The leading
// underscore marks "this happened to the part" rather than "this field
// of the part changed"; it is stripped for the i18n key.
const PART_FIELD_RE = /^parts\[(\d+)\]\.(body|title|lang|_create|_trash|_restore|_purge)$/
// Note-wide structural tags: they name no single part.
const PARTS_STRUCTURAL_RE = /^parts\.(_reorder|_merge_in|_merge_out)$/

/** Humanise a single ``changed_fields`` token for the timeline label.
 *
 * Part edits arrive tagged ``parts[N].body|title|lang`` (N = the part's
 * ``ord``, the ``#N`` chip in the editor) and render as "Part N: body".
 * Rows written before the ord was recorded use the legacy ``parts.body``
 * form (no number). Core columns reuse the diff-table labels; lifecycle
 * tags (``_create`` …) and anything unknown fall back through i18n to the
 * raw token. */
function prettyField(token: string, t: TFunction): string {
  const m = PART_FIELD_RE.exec(token)
  if (m)
    return t(`revisions.fields.part_${m[2].replace(/^_/, '')}`, {
      n: m[1],
      defaultValue: token,
    })
  const s = PARTS_STRUCTURAL_RE.exec(token)
  if (s)
    return t(`revisions.fields.parts${s[1]}`, { defaultValue: token })
  if (token === 'parts.body' || token === 'parts.title' || token === 'parts.lang')
    return t(`revisions.fields.${token.replace('.', '_')}_noord`, {
      defaultValue: token,
    })
  // Core columns + lifecycle tags (``_create`` …): try the dedicated
  // ``revisions.fields`` keys first, then fall back to the diff-table
  // labels (which already name every task/note column), then the raw
  // token. This keeps task revisions readable too (e.g. ``importance``
  // → "Importance") without duplicating the column dictionary.
  return t(`revisions.fields.${token}`, {
    defaultValue: t(`revisions.diff.fields.${token}`, { defaultValue: token }),
  })
}

function prettyChangedFields(fields: readonly string[], t: TFunction): string {
  // Lifecycle transitions (create / archive / delete / restore) tag the
  // revision with a leading ``_<action>`` PLUS the column they touched
  // (``_archive`` alongside ``is_archived``, ``_delete`` alongside
  // ``deleted_at`` …). The action label already names the event, so the
  // co-emitted column is redundant noise: when any ``_<action>`` tag is
  // present, show only those.
  const lifecycle = fields.filter((f) => f.startsWith('_'))
  const shown = lifecycle.length > 0 ? lifecycle : fields
  return shown.map((f) => prettyField(f, t)).join(', ')
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
  // The history panel is rarely consulted; default it to collapsed so
  // the task/note detail doesn't end with a long list pushing the
  // primary actions out of view.
  const [expanded, setExpanded] = useState(false)
  // Inline editing of the summary label: only one row at a time. The
  // draft holds keystrokes; blur (or Enter) saves via PATCH, Esc
  // cancels. ``null`` means "no row in edit mode".
  const [editingId, setEditingId] = useState<string | null>(null)
  const [draft, setDraft] = useState('')

  const saveSummary = useCallback(
    async (rev: Revision, raw: string) => {
      const trimmed = raw.trim()
      const value: string | null = trimmed === '' ? null : trimmed
      if (value === (rev.summary ?? null)) {
        // No change: just exit the editor.
        setEditingId(null)
        return
      }
      setErr(null)
      const path =
        kind === 'task'
          ? '/tasks/{task_id}/revisions/{rev_id}'
          : '/notes/{note_id}/revisions/{rev_id}'
      const params =
        kind === 'task'
          ? { header: workspaceHeader(), path: { task_id: id, rev_id: rev.id } }
          : { header: workspaceHeader(), path: { note_id: id, rev_id: rev.id } }
      const { data, error } =
        kind === 'task'
          ? await api.PATCH(path as '/tasks/{task_id}/revisions/{rev_id}', {
              // openapi-fetch type narrowing trips on the union; the
              // runtime values are valid against either branch.
              // eslint-disable-next-line @typescript-eslint/no-explicit-any
              params: params as any,
              body: { summary: value },
            })
          : await api.PATCH(path as '/notes/{note_id}/revisions/{rev_id}', {
              // eslint-disable-next-line @typescript-eslint/no-explicit-any
              params: params as any,
              body: { summary: value },
            })
      if (error || !data) {
        setErr(errMessage(error))
        return
      }
      setRows((cur) =>
        cur.map((r) => (r.id === rev.id ? { ...r, summary: data.summary } : r)),
      )
      setEditingId(null)
    },
    [kind, id],
  )

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
      <button
        type="button"
        className="revisions-panel__toggle"
        aria-expanded={expanded}
        onClick={() => setExpanded((v) => !v)}
      >
        <span className="revisions-panel__caret" aria-hidden="true">
          {expanded ? '▾' : '▸'}
        </span>
        <h3>{t('revisions.title')}</h3>
        {!expanded && rows.length > 0 && (
          <span className="muted">({rows.length})</span>
        )}
      </button>
      {err && <p className="error">{err}</p>}
      {expanded && rows.length === 0 && (
        <p className="muted">{t('revisions.empty')}</p>
      )}
      {expanded && rows.length > 0 && (
        <ul className="revisions-list">
          {rows.map((rev) => {
            const open = rev.sealed_at === null
            const ts = open ? rev.last_edit_at : rev.sealed_at
            const channelLabel = t(`revisions.channel.${rev.channel}`, {
              defaultValue: rev.channel,
            })
            const fields = prettyChangedFields(rev.changed_fields, t)
            const isSelected = rev.id === selectedId
            const isEditingLabel = editingId === rev.id
            const labelDisplay = rev.summary ?? fields
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
                    {rev.seq != null && (
                      <span
                        className="revision-version"
                        title={t('revisions.versionTitle', {
                          defaultValue: 'Revision',
                        })}
                      >
                        v{rev.seq}
                      </span>
                    )}
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
                </button>
                {/* Editable summary label, sibling of the expand-button
                 *   so the input isn't a nested-interactive inside it.
                 *   When the row is open (active editing session) the
                 *   label is suppressed: no sealed snapshot to label. */}
                {!open && (
                  <div className="revision-label">
                    {isEditingLabel ? (
                      <input
                        autoFocus
                        className="revision-label__input"
                        type="text"
                        maxLength={200}
                        value={draft}
                        placeholder={fields || t('revisions.labelPh')}
                        onChange={(e) => setDraft(e.target.value)}
                        onBlur={() => void saveSummary(rev, draft)}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter') {
                            e.preventDefault()
                            void saveSummary(rev, draft)
                          } else if (e.key === 'Escape') {
                            e.preventDefault()
                            setEditingId(null)
                          }
                        }}
                      />
                    ) : (
                      <button
                        type="button"
                        className={
                          'revision-label__text' +
                          (rev.summary ? '' : ' revision-label__text--fallback')
                        }
                        title={t('revisions.labelEdit')}
                        onClick={() => {
                          setEditingId(rev.id)
                          setDraft(rev.summary ?? '')
                        }}
                      >
                        {labelDisplay || t('revisions.labelPh')}
                      </button>
                    )}
                  </div>
                )}
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
