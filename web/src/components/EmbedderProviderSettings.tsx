import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api, errCode, errMessage, workspaceHeader } from '../api/client'
import type { components } from '../shared'

type Provider = components['schemas']['EmbedderProviderOut']

// Embedder providers (org_embedder_provider CHECK). Hosted = Scaleway.
const PROVIDERS = ['local', 'scaleway'] as const

// Platform-admin only (rendered behind the is_admin + admin-mode gate in
// Settings). Selects the org's HOSTED embedder tier (the optional
// embedding_hosted halfvec column); the LOCAL tier (bge-m3) is always on.
// A hosted model MUST emit the fleet hosted dim (4000); a key/model that
// can't is fail-closed rejected server-side. Mirrors LlmProviderSettings.
export function EmbedderProviderSettings() {
  const { t } = useTranslation()
  const [cur, setCur] = useState<Provider | null>(null)
  const [provider, setProvider] = useState<string>('local')
  const [model, setModel] = useState('')
  const [baseUrl, setBaseUrl] = useState('')
  const [keyMode, setKeyMode] = useState<'our' | 'byok'>('our')
  const [apiKey, setApiKey] = useState('')
  const [models, setModels] = useState<string[]>([])
  const [err, setErr] = useState<string | null>(null)
  const [msg, setMsg] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [tick, setTick] = useState(0)
  // Gate interaction until the initial fetch populates the form, so a
  // provider change made before it returns is not silently reverted by the
  // late ``setProvider(data.provider)`` (same init race as LlmProviderSettings).
  const [loaded, setLoaded] = useState(false)

  useEffect(() => {
    let active = true
    void (async () => {
      const { data, error } = await api.GET('/embedder-provider', {
        params: { header: workspaceHeader() },
      })
      if (!active) return
      if (error) {
        setErr(errMessage(error))
      } else if (data) {
        setCur(data)
        setProvider(data.provider)
        setModel(data.model ?? '')
        setBaseUrl(data.base_url ?? '')
        setKeyMode(data.has_key ? 'byok' : 'our')
        if (data.provider === 'scaleway') {
          const r = await api.GET('/embedder-provider/scaleway/models', {
            params: { header: workspaceHeader() },
          })
          if (active && !r.error) setModels(r.data?.models ?? [])
        }
      }
      if (active) setLoaded(true)
    })()
    return () => {
      active = false
    }
  }, [tick])

  async function loadModels() {
    setErr(null)
    const { data, error } = await api.GET('/embedder-provider/scaleway/models', {
      params: { header: workspaceHeader() },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    setModels(data?.models ?? [])
  }

  function onProviderChange(next: string) {
    setProvider(next)
    setModels([])
    if (next === 'scaleway') void loadModels()
  }

  const hosted = provider !== 'local'
  const needsKey = hosted && keyMode === 'byok' && !apiKey.trim() && !cur?.has_key

  function keyForSave(): string | null {
    if (keyMode === 'byok') return apiKey.trim() ? apiKey.trim() : null
    return ''
  }

  async function save() {
    setErr(null)
    setMsg(null)
    if (needsKey) {
      setErr(t('llmp.needKey'))
      return
    }
    setBusy(true)
    const { data, error } = await api.PUT('/embedder-provider', {
      params: { header: workspaceHeader() },
      body: {
        provider,
        model: hosted && model.trim() ? model.trim() : null,
        base_url: hosted && baseUrl.trim() ? baseUrl.trim() : null,
        api_key: keyForSave(),
      },
    })
    setBusy(false)
    if (error) {
      setErr(
        errCode(error) === 'provider.key_invalid'
          ? t('emp.keyInvalid')
          : errMessage(error),
      )
      return
    }
    if (data) setCur(data)
    setApiKey('')
    setMsg(t('llmp.saved'))
    setTick((n) => n + 1)
  }

  return (
    <section className="card">
      <h2>{t('emp.title')}</h2>
      <p className="hint">{t('emp.intro')}</p>
      {cur && (
        <p className="muted">
          {cur.provider === 'scaleway'
            ? t('emp.current_hosted', { model: cur.model || t('emp.modelDefault') })
            : t('emp.current_local')}
        </p>
      )}
      {err && <p className="err">{err}</p>}
      {msg && <p className="ok">{msg}</p>}

      <label>
        {t('emp.provider')}
        <select
          value={provider}
          disabled={!loaded}
          onChange={(e) => onProviderChange(e.target.value)}
        >
          {PROVIDERS.map((p) => (
            <option key={p} value={p}>
              {t(`emp.provider_${p}`)}
            </option>
          ))}
        </select>
      </label>

      {hosted && (
        <>
          <label>
            {t('llmp.model')}
            <input
              value={model}
              list="scw-embed-models"
              placeholder={t('llmp.modelPlaceholder')}
              onChange={(e) => setModel(e.target.value)}
            />
            <datalist id="scw-embed-models">
              {models.map((m) => (
                <option key={m} value={m} />
              ))}
            </datalist>
          </label>

          <label>
            {t('llmp.baseUrl')}
            <input
              value={baseUrl}
              placeholder={t('llmp.baseUrlPlaceholder')}
              onChange={(e) => setBaseUrl(e.target.value)}
            />
          </label>

          <fieldset className="llmp__keymode">
            <legend>{t('llmp.keyMode')}</legend>
            <label className="row">
              <input
                type="radio"
                name="emp-keymode"
                checked={keyMode === 'our'}
                onChange={() => setKeyMode('our')}
              />
              {t('llmp.useOurKey')}
            </label>
            <label className="row">
              <input
                type="radio"
                name="emp-keymode"
                checked={keyMode === 'byok'}
                onChange={() => setKeyMode('byok')}
              />
              {t('llmp.useOwnKey')}
            </label>
          </fieldset>

          {keyMode === 'byok' && (
            <label>
              {t('llmp.apiKey')}
              <input
                type="password"
                value={apiKey}
                autoComplete="off"
                placeholder={
                  cur?.has_key ? t('llmp.keySetPlaceholder') : t('llmp.apiKeyPlaceholder')
                }
                onChange={(e) => setApiKey(e.target.value)}
              />
              <span className="muted">
                {cur?.has_key ? t('llmp.keySet') : t('llmp.noKey')}
              </span>
            </label>
          )}
        </>
      )}

      <div className="row">
        <button type="button" disabled={busy || !loaded} onClick={() => void save()}>
          {t('llmp.save')}
        </button>
      </div>
    </section>
  )
}
