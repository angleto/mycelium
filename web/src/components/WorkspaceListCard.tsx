import { useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api, errMessage } from '../api/client'
import { switchWorkspace } from '../auth/session'
import { useSession } from '../auth/useSession'
import { reloadWorkspaces, useWorkspaces } from '../auth/useWorkspaces'
import {
  ARCHIVED,
  archivedWorkspaces,
  canManageWorkspace,
  deleteBlockedReason,
  fallbackWorkspaceId,
  switchableWorkspaces,
  type WorkspaceChoice,
} from '../lib/workspaceChoice'
import { ConfirmDialog } from './ConfirmDialog'
import type { components } from '../shared'

type Summary = components['schemas']['WorkspaceSummaryOut']

// Settings → Workspace: every workspace you belong to, and its
// lifecycle — create, switch, archive, restore, delete.
//
// These four endpoints are PRE-TENANT (`/workspaces`, `/workspaces/{id}
// /archive|unarchive`, `DELETE /workspaces/{id}`): they carry no
// workspace header and the server checks the RAW membership role of the
// row, not the "acting as" role. So the buttons are gated on `w.role`,
// and an owner does NOT have to raise the mode chip to archive or
// delete — gating them on the elevated role would have disabled a
// control the server would happily accept.
//
// Archiving is reversible and hides the workspace from the switcher;
// deleting is not reversible and takes every note, task, invoice and
// attachment with it, which is why the two live behind very different
// affordances: a plain button vs. a dialog that makes you type the name.
export function WorkspaceListCard() {
  const { t } = useTranslation()
  const session = useSession()
  const activeId = session?.workspaceId ?? null
  const { list } = useWorkspaces()

  const [newName, setNewName] = useState('')
  const [showArchived, setShowArchived] = useState(false)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [deleting, setDeleting] = useState<Summary | null>(null)
  const [archiving, setArchiving] = useState<Summary | null>(null)
  const [dialogErr, setDialogErr] = useState<string | null>(null)

  const all = list ?? []
  const live = switchableWorkspaces(all, activeId)
  const archivedList = archivedWorkspaces(all, activeId)
  // Named in the archive confirmation, so "you will be moved" is not a
  // surprise about WHERE.
  const archiveDestination = archiving
    ? (all.find((w) => w.id === fallbackWorkspaceId(all, archiving.id))?.name ?? null)
    : null

  async function onCreate(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setErr(null)
    const { data, error } = await api.POST('/workspaces', {
      body: { name: newName.trim() },
    })
    setBusy(false)
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    setNewName('')
    await reloadWorkspaces()
    switchWorkspace(data.id)
  }

  async function setArchived(w: Summary, archived: boolean) {
    setBusy(true)
    setErr(null)
    const path = { params: { path: { workspace_id: w.id } } }
    const { error } = archived
      ? await api.POST('/workspaces/{workspace_id}/archive', path)
      : await api.POST('/workspaces/{workspace_id}/unarchive', path)
    setBusy(false)
    if (error) {
      setErr(errMessage(error))
      return
    }
    // Compute the destination from the roster we still hold, refresh it,
    // and only THEN move: switching remounts the whole authenticated
    // subtree, and doing it first would render the new context from a
    // roster that still describes the old state.
    const next =
      archived && w.id === activeId ? fallbackWorkspaceId(all, w.id) : null
    setArchiving(null)
    await reloadWorkspaces()
    // You archived the workspace you were standing in. Archiving means
    // "put this away", so leave it — but only after saying so, and only
    // when there is somewhere to go: the backend keeps an archived
    // workspace fully usable, so with no alternative we stay put (the
    // identity card then explains the state) rather than strand the
    // session.
    if (next) switchWorkspace(next)
  }

  async function onDelete(w: Summary) {
    setBusy(true)
    setDialogErr(null)
    const { error } = await api.DELETE('/workspaces/{workspace_id}', {
      params: { path: { workspace_id: w.id } },
    })
    setBusy(false)
    if (error) {
      setDialogErr(errMessage(error))
      return
    }
    // Order matters, in both directions.
    //
    // The session still points at a workspace that no longer exists, and
    // `workspaceHeader()` would keep sending that id: every tenant-scoped
    // request would 403 forever, and a 403 is not a 401 so nothing clears
    // the session. So we must move.
    //
    // But the roster has to be refreshed BEFORE the move, not after:
    // switching remounts the whole authenticated subtree, and the
    // remounted switcher would otherwise list the workspace we just
    // deleted — a ghost row that activates a dead tenant when clicked.
    const next = w.id === activeId ? fallbackWorkspaceId(all, w.id) : null
    setDeleting(null)
    await reloadWorkspaces()
    if (next) switchWorkspace(next)
  }

  function row(w: Summary) {
    const manage = canManageWorkspace(w as WorkspaceChoice)
    const blocked = deleteBlockedReason(all as WorkspaceChoice[], w as WorkspaceChoice)
    return (
      <li key={w.id} className="cpitem">
        <div className="cpitem__head">
          <span className="cpitem__name">{w.name}</span>
          <span className="cpmeta">
            <span className="tag tag--muted">{t(`roles.${w.role}`)}</span>
            {w.status === ARCHIVED && (
              <span className="tag tag--muted">{t('wsmgr.archived')}</span>
            )}
            {w.id === activeId && <span className="tag">{t('wsmgr.current')}</span>}
          </span>
          <span className="grow" />
          {w.id !== activeId && (
            <button
              type="button"
              className="btn--ghost btn--sm"
              disabled={busy}
              onClick={() => switchWorkspace(w.id)}
            >
              {t('wsmgr.switch')}
            </button>
          )}
          <button
            type="button"
            className="btn--ghost btn--sm"
            disabled={busy || !manage}
            title={manage ? undefined : t('wsmgr.ownerOnly')}
            onClick={() => {
              // Archiving the workspace you are standing in moves you
              // out of it: ask first, and name where you are going.
              if (w.status !== ARCHIVED && w.id === activeId) {
                setArchiving(w)
                return
              }
              void setArchived(w, w.status !== ARCHIVED)
            }}
          >
            {w.status === ARCHIVED ? t('wsmgr.unarchive') : t('wsmgr.archive')}
          </button>
          <button
            type="button"
            className="btn--danger btn--sm"
            disabled={busy || blocked !== null}
            title={
              blocked === 'sole'
                ? t('wsmgr.soleHint')
                : blocked === 'not_owner'
                  ? t('wsmgr.ownerOnly')
                  : undefined
            }
            onClick={() => {
              setDialogErr(null)
              setDeleting(w)
            }}
          >
            {t('wsmgr.delete')}
          </button>
        </div>
      </li>
    )
  }

  return (
    <section className="card">
      <h2>{t('wsmgr.yours')}</h2>
      <p className="hint">{t('wsmgr.yoursNote')}</p>
      {err && <p className="err">{err}</p>}

      {list === null ? (
        <p>{t('wsmgr.loading')}</p>
      ) : (
        <ul className="list">{live.map(row)}</ul>
      )}

      {archivedList.length > 0 && (
        <>
          <label className="row">
            <input
              type="checkbox"
              checked={showArchived}
              onChange={(e) => setShowArchived(e.target.checked)}
            />
            {t('wsmgr.showArchived', { count: archivedList.length })}
          </label>
          {showArchived && <ul className="list">{archivedList.map(row)}</ul>}
        </>
      )}

      <form onSubmit={(e) => void onCreate(e)} className="row">
        <input
          required
          placeholder={t('wsmgr.newName')}
          value={newName}
          onChange={(e) => setNewName(e.target.value)}
        />
        <button type="submit" disabled={busy || !newName.trim()}>
          {t('wsmgr.create')}
        </button>
      </form>

      {archiving && (
        <ConfirmDialog
          title={t('wsmgr.archive')}
          intro={t('wsmgr.confirmArchive', { name: archiving.name })}
          confirmLabel={t('wsmgr.archive')}
          busy={busy}
          error={err}
          onConfirm={() => void setArchived(archiving, true)}
          onClose={() => setArchiving(null)}
        >
          <p className="hint">
            {archiveDestination
              ? t('wsmgr.archiveMovesYou', { name: archiveDestination })
              : t('wsmgr.archiveStayHere')}
          </p>
        </ConfirmDialog>
      )}

      {deleting && (
        <ConfirmDialog
          title={t('wsmgr.deleteTitle', { name: deleting.name })}
          intro={t('wsmgr.confirmDelete', { name: deleting.name })}
          confirmLabel={t('wsmgr.deleteConfirm')}
          confirmWord={deleting.name}
          confirmWordHint={t('wsmgr.typeToConfirm', { name: deleting.name })}
          danger
          busy={busy}
          error={dialogErr}
          onConfirm={() => void onDelete(deleting)}
          onClose={() => setDeleting(null)}
        >
          <ul className="confirm__what">
            <li>{t('wsmgr.deleteWhat.content')}</li>
            <li>{t('wsmgr.deleteWhat.billing')}</li>
            <li>{t('wsmgr.deleteWhat.files')}</li>
            <li>{t('wsmgr.deleteWhat.members')}</li>
          </ul>
          <p className="hint">
            {deleting.id === activeId
              ? t('wsmgr.deleteActiveNote')
              : t('wsmgr.deleteArchiveInstead')}
          </p>
        </ConfirmDialog>
      )}
    </section>
  )
}
