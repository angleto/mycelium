import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api, errCode, errMessage, workspaceHeader } from '../api/client'
import type { components } from '../api/schema'

type Provider = components['schemas']['LLMProviderOut']

// Provider kinds the resolver understands (org_llm_provider CHECK).
const PROVIDERS = ['local', 'openai', 'anthropic', 'scaleway'] as const

// Platform-admin only (rendered behind the is_admin + admin-mode gate in
// Settings). Selects the org's LLM provider behind the per-org seam:
// local (bundled), or a hosted provider on our key (our_key) or the org's
// own key (BYOK). The stored key is never returned by the API (only
// has_key); a new key is fail-closed probed server-side before it is
// persisted. The curated Scaleway roster is validated against live
// /v1/models. NOT the same as AiAssistantsSettings (which labels external
// MCP-client identities and does not back the runtime LLM). (task d2c60a83)
export function LlmProviderSettings() {
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

  useEffect(() => {
    let active = true
    void (async () => {
      const { data, error } = await api.GET('/llm-provider', {
        params: { header: workspaceHeader() },
      })
      if (!active) return
      if (error) {
        setErr(errMessage(error))
        return
      }
      if (data) {
        setCur(data)
        setProvider(data.provider)
        setModel(data.model ?? '')
        setBaseUrl(data.base_url ?? '')
        setKeyMode(data.has_key ? 'byok' : 'our')
        if (data.provider === 'scaleway') {
          const r = await api.GET('/llm-provider/scaleway/models', {
            params: { header: workspaceHeader() },
          })
          if (active && !r.error) setModels(r.data?.models ?? [])
        }
      }
    })()
    return () => {
      active = false
    }
  }, [tick])

  async function loadScalewayModels() {
    setErr(null)
    const { data, error } = await api.GET('/llm-provider/scaleway/models', {
      params: { header: workspaceHeader() },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    setModels(data?.models ?? [])
  }

  // Event-driven (not an effect): switching to Scaleway loads the curated
  // roster; switching away clears it.
  function onProviderChange(next: string) {
    setProvider(next)
    setModels([])
    if (next === 'scaleway') void loadScalewayModels()
  }

  const hosted = provider !== 'local'
  // BYOK selected but no usable key (none typed and none stored) -> block.
  const needsKey = hosted && keyMode === 'byok' && !apiKey.trim() && !cur?.has_key

  // api_key semantics on PUT: a value sets a new BYOK key (probed),
  // ``null`` leaves the stored key untouched, ``""`` clears it (our_key).
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
    const { data, error } = await api.PUT('/llm-provider', {
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
          ? t('llmp.keyInvalid')
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
      <h2>{t('llmp.title')}</h2>
      <p className="hint">{t('llmp.intro')}</p>
      {err && <p className="err">{err}</p>}
      {msg && <p className="ok">{msg}</p>}

      <label>
        {t('llmp.provider')}
        <select value={provider} onChange={(e) => onProviderChange(e.target.value)}>
          {PROVIDERS.map((p) => (
            <option key={p} value={p}>
              {t(`llmp.provider_${p}`)}
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
              list={provider === 'scaleway' ? 'scw-models' : undefined}
              placeholder={t('llmp.modelPlaceholder')}
              onChange={(e) => setModel(e.target.value)}
            />
            {provider === 'scaleway' && (
              <datalist id="scw-models">
                {models.map((m) => (
                  <option key={m} value={m} />
                ))}
              </datalist>
            )}
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
                name="keymode"
                checked={keyMode === 'our'}
                onChange={() => setKeyMode('our')}
              />
              {t('llmp.useOurKey')}
            </label>
            <label className="row">
              <input
                type="radio"
                name="keymode"
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
        <button type="button" disabled={busy} onClick={() => void save()}>
          {t('llmp.save')}
        </button>
      </div>

      <ByokFactor />
    </section>
  )
}

// BYOK platform fee: the minimal per-token percentage Flow charges when an
// org brings its own key. Write-only (no GET endpoint exposes the current
// value); the server defaults it per-org. Admin-gated like the parent.
function ByokFactor() {
  const { t } = useTranslation()
  const [factor, setFactor] = useState('')
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function save() {
    setErr(null)
    setMsg(null)
    const n = Number(factor)
    if (!factor.trim() || Number.isNaN(n) || n < 0) {
      setErr(t('llmp.factorInvalid'))
      return
    }
    setBusy(true)
    const { error } = await api.PUT('/billing/byok-factor', {
      params: { header: workspaceHeader() },
      body: { factor: n },
    })
    setBusy(false)
    if (error) {
      setErr(errMessage(error))
      return
    }
    setMsg(t('llmp.saved'))
  }

  return (
    <details className="llmp__advanced">
      <summary>{t('llmp.byokFactorTitle')}</summary>
      <p className="hint">{t('llmp.byokFactorHint')}</p>
      {err && <p className="err">{err}</p>}
      {msg && <p className="ok">{msg}</p>}
      <div className="row">
        <input
          type="number"
          step="0.0001"
          min="0"
          value={factor}
          placeholder="0.0001"
          onChange={(e) => setFactor(e.target.value)}
        />
        <button type="button" disabled={busy} onClick={() => void save()}>
          {t('llmp.saveFactor')}
        </button>
      </div>
    </details>
  )
}
