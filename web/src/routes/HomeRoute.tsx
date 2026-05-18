import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import { useSession } from '../auth/useSession'
import type { components } from '../api/schema'

type Workspace = components['schemas']['WorkspaceOut']

// Shows the active workspace and demonstrates optimistic concurrency
// (rename sends expected_version; 409 -> reload). Reloads when the
// in-app switcher changes the active workspace.
export function HomeRoute() {
  const { t } = useTranslation()
  const session = useSession()
  const activeId = session?.workspaceId
  const [ws, setWs] = useState<Workspace | null>(null)
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)

  const load = useCallback(async () => {
    setErr(null)
    const { data, error } = await api.GET('/workspaces/me', {
      params: { header: workspaceHeader() },
    })
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    setWs(data)
    setName(data.name)
  }, [])

  useEffect(() => {
    let active = true
    void (async () => {
      const { data, error } = await api.GET('/workspaces/me', {
        params: { header: workspaceHeader() },
      })
      if (!active) return
      if (error || !data) {
        setErr(errMessage(error))
        return
      }
      setWs(data)
      setName(data.name)
    })()
    return () => {
      active = false
    }
  }, [activeId])

  async function onRename(e: FormEvent) {
    e.preventDefault()
    if (!ws) return
    setBusy(true)
    setMsg(null)
    setErr(null)
    const { error, response } = await api.PATCH('/workspaces/me', {
      params: { header: workspaceHeader() },
      body: { name, expected_version: ws.version },
    })
    setBusy(false)
    if (response.status === 409) {
      setErr(t('home.conflict'))
      await load()
      return
    }
    if (error) {
      setErr(errMessage(error))
      return
    }
    await load()
    setMsg(t('home.renamed'))
  }

  if (err && !ws) return <p className="err">{err}</p>
  if (!ws) return <p>{t('home.loading')}</p>

  return (
    <section className="card">
      <h1>{t('home.title')}</h1>
      <dl className="kv">
        <dt>{t('home.id')}</dt>
        <dd>{ws.id}</dd>
        <dt>{t('home.name')}</dt>
        <dd>{ws.name}</dd>
        <dt>{t('home.version')}</dt>
        <dd>{ws.version}</dd>
      </dl>
      <form onSubmit={(e) => void onRename(e)}>
        <h2>{t('home.rename')}</h2>
        <label>
          {t('home.newName')}
          <input
            required
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </label>
        {msg && <p className="ok">{msg}</p>}
        {err && <p className="err">{err}</p>}
        <button type="submit" disabled={busy}>
          {busy ? t('home.saving') : t('home.save')}
        </button>
      </form>
    </section>
  )
}
