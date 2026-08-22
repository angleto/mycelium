import { useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import { useMyWorkspace } from '../auth/useMyWorkspace'
import { useWorkspaceRole } from '../auth/useSession'
import { reloadWorkspaces } from '../auth/useWorkspaces'
import { ARCHIVED, canWriteWorkspace } from '../lib/workspaceChoice'

// Settings → Workspace, first card: WHICH workspace you are configuring
// and its name. Everything below it on that page applies to this
// workspace and nothing else, which is the whole reason the section
// leads with its identity.
//
// The rename is a TENANT-SCOPED privileged write (`PATCH /workspaces/me`
// -> `ensure_role(ctx.role, owner)`), so it is gated on the EFFECTIVE
// role: an owner running at the default least privilege is genuinely
// refused by the server, and the hint says so instead of letting the
// user discover it through a 403.
export function WorkspaceIdentityCard() {
  const { t } = useTranslation()
  const { ws, reload } = useMyWorkspace()
  const requested = useWorkspaceRole()
  const canWrite = canWriteWorkspace(ws?.my_role ?? 'member', requested)

  const [name, setName] = useState('')
  const [nameFor, setNameFor] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)

  // Adjust state during render rather than in an effect (the React
  // idiom): the field follows the workspace it belongs to.
  if (ws && nameFor !== ws.id) {
    setNameFor(ws.id)
    setName(ws.name)
  }

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
      await reload()
      return
    }
    if (error) {
      setErr(errMessage(error))
      return
    }
    setMsg(t('wsmgr.saved'))
    // Both the active-workspace snapshot AND the roster carry the name:
    // refresh both, or the sidebar switcher keeps showing the old one.
    await Promise.all([reload(), reloadWorkspaces()])
  }

  if (!ws) return <p>{t('wsmgr.loading')}</p>

  return (
    <section className="card">
      <h2>{t('wsmgr.title')}</h2>
      {err && <p className="err">{err}</p>}
      {msg && <p className="ok">{msg}</p>}
      {!canWrite && <p className="hint">{t('members.manageHint')}</p>}
      {ws.status === ARCHIVED && (
        <p className="hint">{t('wsmgr.activeArchived')}</p>
      )}

      <form onSubmit={(e) => void onRename(e)}>
        <label>
          {t('wsmgr.id')}
          <input value={ws.id} readOnly onFocus={(e) => e.target.select()} />
        </label>
        <label>
          {t('wsmgr.rename')}
          {/* Stable hook for the e2e that asserts the effective-role gate
              (it used to grab this field positionally, as the first
              non-readonly input on the page, which any new card above it
              would have silently retargeted). */}
          <input
            className="wsident__name"
            required
            value={name}
            disabled={!canWrite}
            onChange={(e) => setName(e.target.value)}
          />
        </label>
        <button type="submit" disabled={busy || !canWrite || name === ws.name}>
          {busy ? t('wsmgr.saving') : t('wsmgr.save')}
        </button>
      </form>
    </section>
  )
}
