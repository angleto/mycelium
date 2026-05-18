import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api, errMessage } from '../api/client'
import { setActiveWorkspace } from '../auth/session'
import { useSession } from '../auth/useSession'
import type { components } from '../api/schema'

type Workspace = components['schemas']['WorkspaceSummaryOut']

// In-app switcher (ADR-0024): switching is just changing the active
// workspace id; no re-auth. Creating a workspace is in-app too.
export function WorkspaceSwitcher() {
  const { t } = useTranslation()
  const session = useSession()
  const [list, setList] = useState<Workspace[]>([])
  const [creating, setCreating] = useState(false)
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

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

  return (
    <div className="switcher">
      <label>
        {t('switcher.label')}{' '}
        <select
          value={session?.workspaceId ?? ''}
          onChange={(e) => setActiveWorkspace(e.target.value)}
        >
          {list.map((w) => (
            <option key={w.id} value={w.id}>
              {w.name}
            </option>
          ))}
        </select>
      </label>
      {creating ? (
        <form onSubmit={(e) => void onCreate(e)} className="switcher__create">
          <input
            required
            placeholder={t('switcher.newName')}
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
          <button type="submit" disabled={busy}>
            {busy ? t('switcher.creating') : t('switcher.create')}
          </button>
        </form>
      ) : (
        <button type="button" onClick={() => setCreating(true)}>
          {t('switcher.create')}
        </button>
      )}
      {err && <span className="err">{err}</span>}
    </div>
  )
}
