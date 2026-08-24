import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { authFetch, errMessage } from '../api/client'

// Platform-admin only (rendered behind the is_admin + admin-mode gate in
// SettingsRoute). Flips the GLOBAL SdI environment (test <-> production) at
// runtime: 'production' makes the next real invoice transmit target the
// production RiceviFile endpoint -- a real fiscal send -- with NO redeploy.
// Untyped authFetch (like the PDF download) so this does not depend on a
// schema.d.ts regen.

type Env = 'test' | 'production'

type SdiEnv = {
  environment: Env
  sdicoop_active: boolean
  test_url: string
  prod_url: string
  active_endpoint: string
  intermediary_id_paese: string
  intermediary_id_codice: string
  intermediary_id_codice_from_settings: boolean
  intermediary_id_codice_warning: string | null
  client_cert_configured: boolean
  client_key_configured: boolean
  ca_bundle_configured: boolean
}

export function SdiSettings() {
  const { t } = useTranslation()
  const [state, setState] = useState<SdiEnv | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [msg, setMsg] = useState<string | null>(null)
  const [code, setCode] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    void (async () => {
      try {
        const res = await authFetch('/admin/sdi-environment')
        if (!active) return
        if (!res.ok) {
          setErr(errMessage(await res.json().catch(() => null)))
          return
        }
        const data = (await res.json()) as SdiEnv
        if (active) {
          setState(data)
          setCode(data.intermediary_id_codice)
        }
      } catch (e) {
        if (active) setErr(String(e))
      }
    })()
    return () => {
      active = false
    }
  }, [])

  async function flip(next: Env) {
    if (busy || state?.environment === next) return
    if (next === 'production' && !window.confirm(t('sdi.confirmProd'))) return
    setBusy(true)
    setErr(null)
    setMsg(null)
    try {
      const res = await authFetch('/admin/sdi-environment', {
        method: 'PUT',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ environment: next }),
      })
      if (!res.ok) {
        setErr(errMessage(await res.json().catch(() => null)))
        return
      }
      setState((await res.json()) as SdiEnv)
      setMsg(t('sdi.switched'))
    } catch (e) {
      setErr(String(e))
    } finally {
      setBusy(false)
    }
  }

  if (state === null) {
    return (
      <section className="card">
        <h2>{t('sdi.title')}</h2>
        {err ? <p className="err">{err}</p> : <p className="hint">…</p>}
      </section>
    )
  }

  async function saveCode() {
    if (busy) return
    setBusy(true)
    setErr(null)
    setMsg(null)
    try {
      const res = await authFetch('/admin/sdi-environment/intermediary', {
        method: 'PUT',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ id_codice: (code ?? '').trim() }),
      })
      if (!res.ok) {
        setErr(errMessage(await res.json().catch(() => null)))
        return
      }
      const data = (await res.json()) as SdiEnv
      setState(data)
      setCode(data.intermediary_id_codice)
      setMsg(t('sdi.codeSaved'))
    } catch (e) {
      setErr(String(e))
    } finally {
      setBusy(false)
    }
  }

  const isProd = state.environment === 'production'
  const presence = (ok: boolean) => (
    <span className={ok ? 'tag' : 'tag tag--muted'}>
      {ok ? t('sdi.present') : t('sdi.missing')}
    </span>
  )
  return (
    <section className="card">
      <h2>{t('sdi.title')}</h2>
      <p className="hint">{t('sdi.hint')}</p>
      {!state.sdicoop_active && <p className="err">{t('sdi.notSdicoop')}</p>}
      {err && <p className="err">{err}</p>}
      {msg && <p className="ok">{msg}</p>}
      <div className="row">
        <button
          type="button"
          className={isProd ? 'btn--sm btn--ghost' : 'btn--sm'}
          disabled={busy || !isProd}
          onClick={() => void flip('test')}
        >
          {t('sdi.useTest')}
        </button>
        <button
          type="button"
          className={isProd ? 'btn--sm' : 'btn--sm btn--ghost'}
          disabled={busy || isProd}
          onClick={() => void flip('production')}
        >
          {t('sdi.useProd')}
        </button>
        <span className={isProd ? 'tag' : 'tag tag--muted'}>
          {isProd ? t('sdi.prodActive') : t('sdi.testActive')}
        </span>
      </div>
      {isProd && <p className="err">{t('sdi.prodWarn')}</p>}
      <p className="hint">
        {t('sdi.activeEndpoint')}: <code>{state.active_endpoint || '—'}</code>
      </p>
      <h3>{t('sdi.channelTitle')}</h3>
      <p className="hint">{t('sdi.channelHint')}</p>
      <dl className="kv">
        <dt>
          <label htmlFor="sdi-id-codice">{t('sdi.idCodice')}</label>
        </dt>
        <dd>
          <input
            id="sdi-id-codice"
            value={code ?? ''}
            maxLength={28}
            spellCheck={false}
            autoComplete="off"
            placeholder={t('sdi.notSet')}
            onChange={(e) => setCode(e.target.value.toUpperCase())}
          />
          <button
            type="button"
            className="btn--sm"
            disabled={busy || (code ?? '') === state.intermediary_id_codice}
            onClick={() => void saveCode()}
          >
            {t('sdi.save')}
          </button>
          {!state.intermediary_id_codice_from_settings && (
            <p className="hint">{t('sdi.codeFromEnv')}</p>
          )}
          {state.intermediary_id_codice_warning === 'physical_person_must_use_16_char_cf' && (
            <p className="err">{t('sdi.codeLooksLikeVat')}</p>
          )}
        </dd>
        <dt>{t('sdi.idPaese')}</dt>
        <dd>
          <code>{state.intermediary_id_paese || t('sdi.notSet')}</code>
        </dd>
        {/* Written out rather than mapped over an array of key strings: the
            pipeline's i18n check only verifies STATIC t('...') calls, and a
            dynamic one would quietly exempt these three from it. */}
        <dt>{t('sdi.clientCert')}</dt>
        <dd>{presence(state.client_cert_configured)}</dd>
        <dt>{t('sdi.clientKey')}</dt>
        <dd>{presence(state.client_key_configured)}</dd>
        <dt>{t('sdi.caBundle')}</dt>
        <dd>{presence(state.ca_bundle_configured)}</dd>
      </dl>
    </section>
  )
}
