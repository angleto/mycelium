import { useEffect, useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import { useSession } from '../auth/useSession'
import type { components } from '../api/schema'

type WS = components['schemas']['WorkspaceOut']

// Settings → Workspaces: just the current workspace's id (read-only)
// and a rename. Nothing else (no list/switch/archive/delete).
export function WorkspaceManager() {
  const { t } = useTranslation()
  const session = useSession()
  const [ws, setWs] = useState<WS | null>(null)
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    void (async () => {
      const { data } = await api.GET('/workspaces/me', {
        params: { header: workspaceHeader() },
      })
      if (active && data) {
        setWs(data)
        setName(data.name)
      }
    })()
    return () => {
      active = false
    }
  }, [session?.workspaceId])

  async function onSave(e: FormEvent) {
    e.preventDefault()
    if (!ws) return
    setBusy(true)
    setErr(null)
    setMsg(null)
    const { data, error, response } = await api.PATCH('/workspaces/me', {
      params: { header: workspaceHeader() },
      body: { name, expected_version: ws.version },
    })
    setBusy(false)
    if (response.status === 409) {
      setErr(t('wsmgr.conflict'))
      const r = await api.GET('/workspaces/me', {
        params: { header: workspaceHeader() },
      })
      if (r.data) {
        setWs(r.data)
        setName(r.data.name)
      }
      return
    }
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    setWs({ ...ws, name, version: data.version })
    setMsg(t('wsmgr.saved'))
  }

  return (
    <section className="card">
      <h2>{t('wsmgr.title')}</h2>
      {err && <p className="err">{err}</p>}
      {msg && <p className="ok">{msg}</p>}
      {ws === null ? (
        <p>{t('wsmgr.loading')}</p>
      ) : (
        <form onSubmit={(e) => void onSave(e)}>
          <label>
            {t('wsmgr.id')}
            <input value={ws.id} readOnly onFocus={(e) => e.target.select()} />
          </label>
          <label>
            {t('wsmgr.rename')}
            <input
              required
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </label>
          <button type="submit" disabled={busy || name === ws.name}>
            {busy ? t('wsmgr.saving') : t('wsmgr.save')}
          </button>
        </form>
      )}
    </section>
  )
}
