import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api, errMessage } from '../api/client'
import { setActiveWorkspace } from '../auth/session'
import { useSession } from '../auth/useSession'
import type { components } from '../api/schema'

type Workspace = components['schemas']['WorkspaceSummaryOut']

// Workspace lifecycle (ADR-0024): archive hides a workspace from the
// switcher by default but keeps it usable; delete is a hard,
// owner-only cascade. Both are pre-tenant (no workspace header).
export function WorkspaceManager() {
  const { t } = useTranslation()
  const session = useSession()
  const activeId = session?.workspaceId
  const [list, setList] = useState<Workspace[] | null>(null)
  const [showArchived, setShowArchived] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    const { data, error } = await api.GET('/workspaces')
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    setList(data)
  }, [])

  useEffect(() => {
    let active = true
    void (async () => {
      const { data } = await api.GET('/workspaces')
      if (active && data) setList(data)
    })()
    return () => {
      active = false
    }
  }, [])

  async function onCreate(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setErr(null)
    const { data, error } = await api.POST('/workspaces', { body: { name } })
    setBusy(false)
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    setName('')
    setCreating(false)
    await load()
    setActiveWorkspace(data.id)
  }

  async function setStatus(id: string, archive: boolean) {
    setErr(null)
    const params = { params: { path: { workspace_id: id } } }
    const { error } = archive
      ? await api.POST('/workspaces/{workspace_id}/archive', params)
      : await api.POST('/workspaces/{workspace_id}/unarchive', params)
    if (error) {
      setErr(errMessage(error))
      return
    }
    await load()
  }

  async function remove(ws: Workspace) {
    if (!window.confirm(t('wsmgr.confirmDelete', { name: ws.name }))) return
    setErr(null)
    const { error } = await api.DELETE('/workspaces/{workspace_id}', {
      params: { path: { workspace_id: ws.id } },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    // Never leave the session pointed at a deleted workspace.
    if (activeId === ws.id) {
      const next = (list ?? []).find((w) => w.id !== ws.id)
      if (next) setActiveWorkspace(next.id)
    }
    await load()
  }

  const visible = (list ?? []).filter(
    (w) => showArchived || w.status !== 'archived',
  )

  return (
    <section className="card">
      <h1>{t('wsmgr.title')}</h1>
      {err && <p className="err">{err}</p>}
      {creating ? (
        <form onSubmit={(e) => void onCreate(e)} className="row">
          <input
            required
            placeholder={t('switcher.newName')}
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
          <button type="submit" disabled={busy}>
            {busy ? t('switcher.creating') : t('switcher.create')}
          </button>
          <button
            type="button"
            className="btn--ghost"
            onClick={() => {
              setCreating(false)
              setName('')
            }}
          >
            {t('wsmgr.cancel')}
          </button>
        </form>
      ) : (
        <button type="button" onClick={() => setCreating(true)}>
          {t('switcher.create')}
        </button>
      )}
      <label className="row">
        <input
          type="checkbox"
          checked={showArchived}
          onChange={(e) => setShowArchived(e.target.checked)}
        />
        {t('wsmgr.showArchived')}
      </label>
      {list === null ? (
        <p>{t('wsmgr.loading')}</p>
      ) : visible.length === 0 ? (
        <p className="hint">{t('wsmgr.none')}</p>
      ) : (
        <ul className="list">
          {visible.map((w) => {
            const archived = w.status === 'archived'
            const isOwner = w.role === 'owner'
            const sole = (list ?? []).length <= 1
            return (
              <li key={w.id}>
                <strong>{w.name}</strong>
                <span className="muted">
                  {' '}
                  {w.role}
                  {archived ? ` · ${t('wsmgr.archived')}` : ''}
                  {w.id === activeId ? ` · ${t('wsmgr.current')}` : ''}
                </span>
                {!archived && w.id !== activeId && (
                  <button
                    type="button"
                    className="btn--sm"
                    onClick={() => setActiveWorkspace(w.id)}
                  >
                    {t('wsmgr.switch')}
                  </button>
                )}
                {archived ? (
                  <button
                    type="button"
                    className="btn--sm"
                    onClick={() => void setStatus(w.id, false)}
                  >
                    {t('wsmgr.unarchive')}
                  </button>
                ) : (
                  <button
                    type="button"
                    className="btn--ghost btn--sm"
                    onClick={() => void setStatus(w.id, true)}
                  >
                    {t('wsmgr.archive')}
                  </button>
                )}
                <button
                  type="button"
                  className="btn--ghost btn--sm"
                  disabled={!isOwner || sole}
                  title={
                    !isOwner
                      ? t('wsmgr.ownerOnly')
                      : sole
                        ? t('wsmgr.soleHint')
                        : undefined
                  }
                  onClick={() => void remove(w)}
                >
                  {t('wsmgr.delete')}
                </button>
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}
