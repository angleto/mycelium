import { type FormEvent, useCallback, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { type OAuthClient, oauthApi, parseRedirectUris } from '../lib/aiAssistants'

// OAuth clients registered by hand (ADR-0069).
//
// Most MCP hosts register themselves and need nothing from this card: the
// server URL is enough. Some instead ask for an "OAuth client ID" to be
// pasted in. This is where one is made: a name and the redirect URIs the host
// will use, and the page shows the client id to copy. There is no secret --
// the client is public and PKCE binds its codes -- so the host's secret
// field stays empty.
//
// A client grants nothing. The credential is created when somebody approves
// a request from it on the consent page, which is why removing a client here
// stops new connections and does NOT revoke the assistants earlier consents
// created; the card says so rather than letting it be discovered.

export function OAuthClientsSettings() {
  const { t } = useTranslation()
  const [clients, setClients] = useState<OAuthClient[] | null>(null)
  const [name, setName] = useState('')
  const [uris, setUris] = useState('')
  const [created, setCreated] = useState<OAuthClient | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const reload = useCallback(async () => {
    try {
      setClients(await oauthApi.listClients())
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
      setClients([])
    }
  }, [])

  useEffect(() => {
    let live = true
    void (async () => {
      try {
        const rows = await oauthApi.listClients()
        if (live) setClients(rows)
      } catch (e) {
        if (!live) return
        setErr(e instanceof Error ? e.message : String(e))
        setClients([])
      }
    })()
    return () => {
      live = false
    }
  }, [])

  async function onCreate(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setErr(null)
    try {
      const row = await oauthApi.createClient(name.trim(), parseRedirectUris(uris))
      setCreated(row)
      setName('')
      setUris('')
      await reload()
    } catch (ex) {
      setErr(ex instanceof Error ? ex.message : String(ex))
    } finally {
      setBusy(false)
    }
  }

  async function onRemove(c: OAuthClient) {
    if (!window.confirm(t('oauthClients.removeConfirm', { name: c.client_name }))) return
    setBusy(true)
    setErr(null)
    try {
      await oauthApi.removeClient(c.client_id)
      if (created?.client_id === c.client_id) setCreated(null)
      await reload()
    } catch (ex) {
      setErr(ex instanceof Error ? ex.message : String(ex))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="aioauth">
      <h3>{t('oauthClients.title')}</h3>
      <p className="hint">{t('oauthClients.intro')}</p>

      {created && (
        <div className="airreveal" role="status">
          <p>{t('oauthClients.createdHint')}</p>
          <div className="row airreveal__row">
            <span className="muted">{t('oauthClients.clientId')}</span>
            <code className="aiconnector__url">
              {created.client_id}
            </code>
            <button
              type="button"
              className="btn--ghost btn--sm"
              onClick={() => void navigator.clipboard?.writeText(created.client_id)}
            >
              {t('aiAssistants.copy')}
            </button>
          </div>
          <p className="hint">{t('oauthClients.noSecret')}</p>
        </div>
      )}

      <form className="aiform" onSubmit={(e) => void onCreate(e)}>
        <label className="aiform__full">
          <span className="muted">{t('oauthClients.nameLabel')}</span>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            required
            maxLength={100}
          />
        </label>
        <label className="aiform__full">
          <span className="muted">{t('oauthClients.urisLabel')}</span>
          <textarea
            value={uris}
            onChange={(e) => setUris(e.target.value)}
            rows={3}
            required
            placeholder={t('oauthClients.urisPlaceholder')}
            spellCheck={false}
          />
        </label>
        <p className="hint">{t('oauthClients.urisHint')}</p>
        {err && (
          <p className="err" role="alert">
            {err}
          </p>
        )}
        <div className="row aiform__actions">
          <button
            type="submit"
            className="btn"
            disabled={busy || !name.trim() || parseRedirectUris(uris).length === 0}
          >
            {t('oauthClients.create')}
          </button>
        </div>
      </form>

      {clients === null && <p className="hint">{t('common.loading')}</p>}
      {clients !== null && clients.length === 0 && (
        <p className="hint">{t('oauthClients.empty')}</p>
      )}
      {clients?.map((c) => (
        <div key={c.client_id} className="airow">
          <div className="airow__head">
            <strong>{c.client_name}</strong>
            <code className="muted">{c.client_id}</code>
            <span className="grow" />
            <button
              type="button"
              className="btn--ghost btn--sm btn--danger"
              disabled={busy}
              onClick={() => void onRemove(c)}
            >
              {t('oauthClients.remove')}
            </button>
          </div>
          <div className="airow__meta muted">{c.redirect_uris.join(', ')}</div>
        </div>
      ))}
      <p className="hint">{t('oauthClients.removeHint')}</p>
    </div>
  )
}
