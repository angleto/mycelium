import { useEffect, useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import { setActiveWorkspace } from '../auth/session'
import { useSession } from '../auth/useSession'
import type { components } from '../api/schema'

type WS = components['schemas']['WorkspaceOut']
type Summary = components['schemas']['WorkspaceSummaryOut']

// Settings → Workspace: the current workspace's id (read-only) + a
// rename; a switch dropdown; and a create. No archive/delete clutter.
export function WorkspaceManager() {
  const { t } = useTranslation()
  const session = useSession()
  const activeId = session?.workspaceId
  const [ws, setWs] = useState<WS | null>(null)
  const [list, setList] = useState<Summary[]>([])
  const [name, setName] = useState('')
  const [newName, setNewName] = useState('')
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)

  async function reload() {
    const me = await api.GET('/workspaces/me', {
      params: { header: workspaceHeader() },
    })
    if (me.data) {
      setWs(me.data)
      setName(me.data.name)
    }
    const all = await api.GET('/workspaces')
    if (all.data) setList(all.data)
  }

  useEffect(() => {
    let active = true
    void (async () => {
      const me = await api.GET('/workspaces/me', {
        params: { header: workspaceHeader() },
      })
      const all = await api.GET('/workspaces')
      if (!active) return
      if (me.data) {
        setWs(me.data)
        setName(me.data.name)
      }
      if (all.data) setList(all.data)
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
    const { data, error, response } = await api.PATCH('/workspaces/me', {
      params: { header: workspaceHeader() },
      body: { name, expected_version: ws.version },
    })
    setBusy(false)
    if (response.status === 409) {
      setErr(t('wsmgr.conflict'))
      await reload()
      return
    }
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    setWs({ ...ws, name, version: data.version })
    setMsg(t('wsmgr.saved'))
  }

  async function onCreate(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setErr(null)
    const { data, error } = await api.POST('/workspaces', {
      body: { name: newName },
    })
    setBusy(false)
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    setNewName('')
    setActiveWorkspace(data.id) // switch into the one just created
  }

  return (
    <section className="card">
      <h2>{t('wsmgr.title')}</h2>
      {err && <p className="err">{err}</p>}
      {msg && <p className="ok">{msg}</p>}
      {ws === null ? (
        <p>{t('wsmgr.loading')}</p>
      ) : (
        <>
          <form onSubmit={(e) => void onRename(e)}>
            <label>
              {t('wsmgr.id')}
              <input
                value={ws.id}
                readOnly
                onFocus={(e) => e.target.select()}
              />
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

          <label className="row" style={{ marginTop: '0.6rem' }}>
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

          <form
            onSubmit={(e) => void onCreate(e)}
            className="row"
            style={{ marginTop: '0.6rem' }}
          >
            <input
              required
              placeholder={t('switcher.newName')}
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
            />
            <button type="submit" disabled={busy || !newName}>
              {t('switcher.create')}
            </button>
          </form>
        </>
      )}
    </section>
  )
}
