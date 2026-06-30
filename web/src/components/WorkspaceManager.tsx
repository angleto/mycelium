import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import { setActiveWorkspace } from '../auth/session'
import { useSession, useWorkspaceRole } from '../auth/useSession'
import { useMyWorkspace } from '../auth/useMyWorkspace'
import type { components } from '../api/schema'

type Summary = components['schemas']['WorkspaceSummaryOut']
type Member = components['schemas']['MemberOut']

const RANK: Record<string, number> = {
  guest: 0,
  member: 1,
  admin: 2,
  owner: 3,
}
// The product model is two namespace roles: owner (privileged) and
// member (normal user). Platform admin is global, not a workspace
// role, so it is not offered here.
const ROLES = ['member', 'owner'] as const

// Settings → Workspace (also the /workspace route): the current
// workspace (id + rename), switch, create, archive/delete, and member
// management. Mutations need the admin/owner effective role; the
// server re-checks, this only gates the UI + shows a hint.
export function WorkspaceManager() {
  const { t } = useTranslation()
  const session = useSession()
  const activeId = session?.workspaceId
  const { ws, reload: reloadWs } = useMyWorkspace()
  const chosen = useWorkspaceRole()

  const ceiling = ws?.my_role ?? 'member'
  const requested = chosen || 'member'
  const effective =
    RANK[requested] <= (RANK[ceiling] ?? 1) ? requested : ceiling
  // Privileged namespace ops (members, clients, workflows, billing)
  // are owner-only; a normal member can only use the workspace.
  const canManage = (RANK[effective] ?? 1) >= RANK.owner

  const [list, setList] = useState<Summary[]>([])
  const [members, setMembers] = useState<Member[]>([])
  const [name, setName] = useState('')
  const [nameFor, setNameFor] = useState<string | null>(null)
  const [newWs, setNewWs] = useState('')
  const [mEmail, setMEmail] = useState('')
  const [mRole, setMRole] = useState('member')
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)

  // Reset the rename field when the active workspace changes (the
  // React "adjust state during render" pattern — not an effect).
  if (ws && nameFor !== ws.id) {
    setNameFor(ws.id)
    setName(ws.name)
  }

  const loadAll = useCallback(async () => {
    const all = await api.GET('/workspaces')
    if (all.data) setList(all.data)
    const mem = await api.GET('/workspaces/me/members', {
      params: { header: workspaceHeader() },
    })
    if (mem.data) setMembers(mem.data)
  }, [])

  useEffect(() => {
    let active = true
    void (async () => {
      const all = await api.GET('/workspaces')
      const mem = await api.GET('/workspaces/me/members', {
        params: { header: workspaceHeader() },
      })
      if (!active) return
      if (all.data) setList(all.data)
      if (mem.data) setMembers(mem.data)
    })()
    return () => {
      active = false
    }
  }, [activeId])

  async function onRename(e: FormEvent) {
    e.preventDefault()
    if (!ws) return
    setBusy(true)
    setErr(null)
    setMsg(null)
    const { error, response } = await api.PATCH('/workspaces/me', {
      params: { header: workspaceHeader() },
      body: { name, expected_version: ws.version },
    })
    setBusy(false)
    if (response.status === 409) {
      setErr(t('wsmgr.conflict'))
      await reloadWs()
      return
    }
    if (error) {
      setErr(errMessage(error))
      return
    }
    setMsg(t('wsmgr.saved'))
    await reloadWs()
  }

  async function onCreate(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setErr(null)
    const { data, error } = await api.POST('/workspaces', {
      body: { name: newWs },
    })
    setBusy(false)
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    setNewWs('')
    setActiveWorkspace(data.id)
  }

  async function archive(id: string, archived: boolean) {
    setErr(null)
    const { error } = archived
      ? await api.POST('/workspaces/{workspace_id}/unarchive', {
          params: { path: { workspace_id: id } },
        })
      : await api.POST('/workspaces/{workspace_id}/archive', {
          params: { path: { workspace_id: id } },
        })
    if (error) {
      setErr(errMessage(error))
      return
    }
    await loadAll()
  }

  async function del(w: Summary) {
    if (!window.confirm(t('wsmgr.confirmDelete', { name: w.name }))) return
    setErr(null)
    const { error } = await api.DELETE('/workspaces/{workspace_id}', {
      params: { path: { workspace_id: w.id } },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    await loadAll()
  }

  async function addMember(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setErr(null)
    const { error } = await api.POST('/workspaces/me/members', {
      params: { header: workspaceHeader() },
      body: { email: mEmail.trim(), role: mRole },
    })
    setBusy(false)
    if (error) {
      setErr(errMessage(error))
      return
    }
    setMEmail('')
    await loadAll()
  }

  async function setMemberRole(m: Member, role: string) {
    setErr(null)
    const { error } = await api.PATCH('/workspaces/me/members/{user_id}', {
      params: { header: workspaceHeader(), path: { user_id: m.user_id } },
      body: { role },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    await loadAll()
  }

  async function removeMember(m: Member) {
    if (!window.confirm(t('members.confirmRemove', { email: m.email }))) return
    setErr(null)
    const { error } = await api.DELETE('/workspaces/me/members/{user_id}', {
      params: { header: workspaceHeader(), path: { user_id: m.user_id } },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    await loadAll()
  }

  if (!ws) return <p>{t('wsmgr.loading')}</p>

  return (
    <>
      <section className="card">
        <h2>{t('wsmgr.title')}</h2>
        {err && <p className="err">{err}</p>}
        {msg && <p className="ok">{msg}</p>}
        {!canManage && <p className="hint">{t('members.manageHint')}</p>}

        <form onSubmit={(e) => void onRename(e)}>
          <label>
            {t('wsmgr.id')}
            <input value={ws.id} readOnly onFocus={(e) => e.target.select()} />
          </label>
          <label>
            {t('wsmgr.rename')}
            <input
              required
              value={name}
              disabled={!canManage}
              onChange={(e) => setName(e.target.value)}
            />
          </label>
          <button
            type="submit"
            disabled={busy || !canManage || name === ws.name}
          >
            {busy ? t('wsmgr.saving') : t('wsmgr.save')}
          </button>
        </form>

        <label className="row">
          {t('wsmgr.switch')}
          <select
            value={activeId ?? ''}
            onChange={(e) => setActiveWorkspace(e.target.value)}
          >
            {list
              .filter((w) => w.status !== 'archived' || w.id === activeId)
              .map((w) => (
                <option key={w.id} value={w.id}>
                  {w.name}
                </option>
              ))}
          </select>
        </label>

        <form onSubmit={(e) => void onCreate(e)} className="row">
          <input
            required
            placeholder={t('switcher.newName')}
            value={newWs}
            onChange={(e) => setNewWs(e.target.value)}
          />
          <button type="submit" disabled={busy || !newWs}>
            {t('members.create')}
          </button>
        </form>

        <ul className="list">
          {list.map((w) => (
            <li key={w.id} className="cpitem">
              <div className="cpitem__head">
                <span className="cpitem__name">{w.name}</span>
                <span className="cpmeta">
                  <span className="tag tag--muted">
                    {t(`roles.${w.role}`)}
                  </span>
                  {w.status === 'archived' && (
                    <span className="tag tag--muted">
                      {t('wsmgr.archived')}
                    </span>
                  )}
                  {w.id === activeId && (
                    <span className="tag">{t('wsmgr.current')}</span>
                  )}
                </span>
                <span className="grow" />
                <button
                  type="button"
                  className="btn--ghost btn--sm"
                  onClick={() =>
                    void archive(w.id, w.status === 'archived')
                  }
                >
                  {w.status === 'archived'
                    ? t('wsmgr.unarchive')
                    : t('wsmgr.archive')}
                </button>
                <button
                  type="button"
                  className="btn--danger btn--sm"
                  onClick={() => void del(w)}
                >
                  {t('wsmgr.delete')}
                </button>
              </div>
            </li>
          ))}
        </ul>
      </section>

      <section className="card">
        <h2>{t('members.title')}</h2>
        {members.length === 0 ? (
          <p className="hint">{t('members.none')}</p>
        ) : (
          <table className="tbl">
            <thead>
              <tr>
                <th>{t('members.email')}</th>
                <th>{t('members.role')}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {members.map((m) => (
                <tr key={m.user_id}>
                  <td>{m.email}</td>
                  <td>
                    <select
                      value={m.role}
                      disabled={!canManage}
                      onChange={(e) =>
                        void setMemberRole(m, e.target.value)
                      }
                    >
                      {ROLES.map((r) => (
                        <option key={r} value={r}>
                          {t(`roles.${r}`)}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td>
                    <button
                      type="button"
                      className="btn--danger btn--sm"
                      disabled={!canManage}
                      onClick={() => void removeMember(m)}
                    >
                      {t('members.remove')}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <form onSubmit={(e) => void addMember(e)} className="row">
          <input
            type="email"
            required
            placeholder={t('members.email')}
            value={mEmail}
            disabled={!canManage}
            onChange={(e) => setMEmail(e.target.value)}
          />
          <select
            value={mRole}
            disabled={!canManage}
            onChange={(e) => setMRole(e.target.value)}
          >
            {ROLES.map((r) => (
              <option key={r} value={r}>
                {t(`roles.${r}`)}
              </option>
            ))}
          </select>
          <button type="submit" disabled={busy || !canManage || !mEmail}>
            {busy ? t('members.adding') : t('members.add')}
          </button>
        </form>
      </section>
    </>
  )
}
