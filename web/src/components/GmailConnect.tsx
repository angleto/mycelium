import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api, authFetch, errMessage, workspaceHeader } from '../api/client'
import type { components } from '../shared'

type EmailAccount = components['schemas']['EmailAccountOut']

// Connects the user's Gmail via OAuth (server-side callback at
// /oauth/google/callback redirects back with ?google=connected). The
// /oauth/google/start endpoint is not yet in schema.d.ts, so the start
// hop uses authFetch. schema regen pending.
export function GmailConnect() {
  const { t } = useTranslation()
  const [account, setAccount] = useState<EmailAccount | null>(null)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState<string | null>(null)
  const [redirecting, setRedirecting] = useState(false)
  const [connectedBanner, setConnectedBanner] = useState(false)
  const [tick, setTick] = useState(0)

  useEffect(() => {
    // Detect the OAuth round-trip callback and surface a banner; then
    // clean the URL so a refresh does not re-show the banner.
    const params = new URLSearchParams(window.location.search)
    const isCallback = params.get('google') === 'connected'
    if (isCallback) {
      params.delete('google')
      const next =
        window.location.pathname +
        (params.toString() ? `?${params.toString()}` : '')
      window.history.replaceState({}, '', next)
    }
    let active = true
    void (async () => {
      const { data, error } = await api.GET('/email/accounts', {
        params: { header: workspaceHeader() },
      })
      if (!active) return
      if (isCallback) setConnectedBanner(true)
      if (error) {
        setErr(errMessage(error))
        setLoading(false)
        return
      }
      const gmail = (data ?? []).find((a) => a.provider === 'gmail') ?? null
      setAccount(gmail)
      setLoading(false)
    })()
    return () => {
      active = false
    }
  }, [tick])

  function reload() {
    setLoading(true)
    setErr(null)
    setTick((n) => n + 1)
  }

  async function onConnect() {
    setErr(null)
    setRedirecting(true)
    try {
      const res = await authFetch('/oauth/google/start?scope=gmail')
      if (!res.ok) {
        setRedirecting(false)
        setErr(`HTTP ${res.status}`)
        return
      }
      const body = (await res.json()) as { authorize_url?: string }
      if (!body.authorize_url) {
        setRedirecting(false)
        setErr(t('error.generic'))
        return
      }
      window.location.href = body.authorize_url
    } catch (e) {
      setRedirecting(false)
      setErr(e instanceof Error ? e.message : String(e))
    }
  }

  async function onDisconnect() {
    if (!account) return
    setErr(null)
    const res = await authFetch(`/email/accounts/${account.id}`, {
      method: 'DELETE',
    })
    if (!res.ok) {
      setErr(`HTTP ${res.status}`)
      return
    }
    reload()
  }

  return (
    <section className="card">
      <h2>{t('connectors.gmail.title')}</h2>
      {connectedBanner && (
        <p className="success">{t('connectors.gmail.connectedBanner')}</p>
      )}
      {err && <p className="err">{err}</p>}
      {loading && <p>{t('home.loading')}</p>}
      {!loading && !account && (
        <>
          <p>{t('connectors.gmail.redirectWarning')}</p>
          <button
            type="button"
            onClick={() => void onConnect()}
            disabled={redirecting}
          >
            {t('connectors.gmail.connect')}
          </button>
        </>
      )}
      {!loading && account && (
        <>
          <dl className="kv">
            <dt>{t('connectors.gmail.title')}</dt>
            <dd>
              <code>{account.email_address}</code>
            </dd>
            {account.last_sync_at && (
              <>
                <dt>{t('connectors.gmail.since')}</dt>
                <dd>{new Date(account.last_sync_at).toLocaleString()}</dd>
              </>
            )}
          </dl>
          <button type="button" onClick={() => void onDisconnect()}>
            {t('connectors.gmail.disconnect')}
          </button>
        </>
      )}
    </section>
  )
}
