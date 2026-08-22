import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import { useMyWorkspace } from '../auth/useMyWorkspace'
import { useSession, useWorkspaceRole } from '../auth/useSession'
import { canWriteWorkspace } from '../lib/workspaceChoice'
import { ConfirmDialog } from './ConfirmDialog'
import type { components } from '../api/schema'

type Member = components['schemas']['MemberOut']

// The product model is two namespace roles: owner (privileged) and
// member (normal user). Platform admin is global, not a workspace
// role, so it is not offered here.
const ROLES = ['member', 'owner'] as const

// Settings → Workspace: who else is in this workspace. Member
// management is owner-gated server-side (`ensure_role(ctx.role, owner)`
// on every mutation, re-checked in SQL against the actor), so the same
// effective-role gate as the rename applies here.
export function WorkspaceMembersCard() {
  const { t } = useTranslation()
  const session = useSession()
  const activeId = session?.workspaceId
  const { ws } = useMyWorkspace()
  const requested = useWorkspaceRole()
  const canWrite = canWriteWorkspace(ws?.my_role ?? 'member', requested)

  const [members, setMembers] = useState<Member[]>([])
  const [email, setEmail] = useState('')
  const [role, setRole] = useState('member')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [removing, setRemoving] = useState<Member | null>(null)

  const load = useCallback(async () => {
    const { data } = await api.GET('/workspaces/me/members', {
      params: { header: workspaceHeader() },
    })
    if (data) setMembers(data)
  }, [])

  useEffect(() => {
    let active = true
    void (async () => {
      const { data } = await api.GET('/workspaces/me/members', {
        params: { header: workspaceHeader() },
      })
      if (active && data) setMembers(data)
    })()
    return () => {
      active = false
    }
  }, [activeId])

  async function addMember(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setErr(null)
    const { error } = await api.POST('/workspaces/me/members', {
      params: { header: workspaceHeader() },
      body: { email: email.trim(), role },
    })
    setBusy(false)
    if (error) {
      setErr(errMessage(error))
      return
    }
    setEmail('')
    await load()
  }

  async function setMemberRole(m: Member, next: string) {
    setErr(null)
    const { error } = await api.PATCH('/workspaces/me/members/{user_id}', {
      params: { header: workspaceHeader(), path: { user_id: m.user_id } },
      body: { role: next },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    await load()
  }

  async function removeMember(m: Member) {
    setBusy(true)
    setErr(null)
    const { error } = await api.DELETE('/workspaces/me/members/{user_id}', {
      params: { header: workspaceHeader(), path: { user_id: m.user_id } },
    })
    setBusy(false)
    if (error) {
      setErr(errMessage(error))
      return
    }
    setRemoving(null)
    await load()
  }

  return (
    <section className="card">
      <h2>{t('members.title')}</h2>
      {err && <p className="err">{err}</p>}
      {!canWrite && <p className="hint">{t('members.manageHint')}</p>}
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
                    disabled={!canWrite}
                    onChange={(e) => void setMemberRole(m, e.target.value)}
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
                    disabled={!canWrite}
                    onClick={() => setRemoving(m)}
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
          value={email}
          disabled={!canWrite}
          onChange={(e) => setEmail(e.target.value)}
        />
        <select
          value={role}
          disabled={!canWrite}
          onChange={(e) => setRole(e.target.value)}
        >
          {ROLES.map((r) => (
            <option key={r} value={r}>
              {t(`roles.${r}`)}
            </option>
          ))}
        </select>
        <button type="submit" disabled={busy || !canWrite || !email}>
          {busy ? t('members.adding') : t('members.add')}
        </button>
      </form>

      {removing && (
        <ConfirmDialog
          title={t('members.remove')}
          intro={t('members.confirmRemove', { email: removing.email })}
          confirmLabel={t('members.remove')}
          danger
          busy={busy}
          error={err}
          onConfirm={() => void removeMember(removing)}
          onClose={() => setRemoving(null)}
        >
          <p className="hint">{t('members.removeDetail')}</p>
        </ConfirmDialog>
      )}
    </section>
  )
}
