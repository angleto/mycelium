import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api, errMessage, orgHeader } from '../api/client'
import type { components } from '../api/schema'

type Org = components['schemas']['OrgOut']

// Demonstrates the contract that matters here: optimistic concurrency.
// Renaming sends expected_version; a stale write yields 409 and we
// reload the canonical state instead of trusting the PATCH body shape.
export function HomeRoute() {
  const { t } = useTranslation()
  const [org, setOrg] = useState<Org | null>(null)
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)

  const load = useCallback(async () => {
    setErr(null)
    const { data, error } = await api.GET('/orgs/me', {
      params: { header: orgHeader() },
    })
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    setOrg(data)
    setName(data.name)
  }, [])

  // Initial fetch: state is set only after the await (in the async
  // continuation), never synchronously in the effect body.
  useEffect(() => {
    let active = true
    void (async () => {
      const { data, error } = await api.GET('/orgs/me', {
        params: { header: orgHeader() },
      })
      if (!active) return
      if (error || !data) {
        setErr(errMessage(error))
        return
      }
      setOrg(data)
      setName(data.name)
    })()
    return () => {
      active = false
    }
  }, [])

  async function onRename(e: FormEvent) {
    e.preventDefault()
    if (!org) return
    setBusy(true)
    setMsg(null)
    setErr(null)
    const { error, response } = await api.PATCH('/orgs/me', {
      params: { header: orgHeader() },
      body: { name, expected_version: org.version },
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

  if (err && !org) return <p className="err">{err}</p>
  if (!org) return <p>{t('home.loading')}</p>

  return (
    <section className="card">
      <h1>{t('home.title')}</h1>
      <dl className="kv">
        <dt>{t('home.id')}</dt>
        <dd>{org.id}</dd>
        <dt>{t('home.name')}</dt>
        <dd>{org.name}</dd>
        <dt>{t('home.version')}</dt>
        <dd>{org.version}</dd>
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
