import { useEffect, useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { authFetch } from '../api/client'

// /ai-assistants endpoints exist in v1.2.15; the OpenAPI schema regen
// runs from the live backend (pnpm gen:api) — until then, type the
// payloads inline matching the API contract documented in
// api/src/flow_api/routers/ai_assistants.py.

type Scope = { key: string; category: 'read' | 'write' | 'danger'; label: string; description: string }
type ConnectorInfo = { mcp_url: string; instructions_md: string }
type Assistant = {
  id: string
  label: string
  provider: string | null
  model_id: string | null
  notes: string | null
  scope: string[]
  is_active: boolean
  version: number
  created_at: string
  updated_at: string
  token_prefix: string | null
}
type AssistantCreated = { assistant: Assistant; raw_secret: string }

async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await authFetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init.headers ?? {}) },
  })
  if (!res.ok) {
    const detail = await res.text().catch(() => '')
    throw new Error(`HTTP ${res.status}: ${detail}`)
  }
  return res.json() as Promise<T>
}

export function AiAssistantsSettings() {
  const { t } = useTranslation()
  const [connector, setConnector] = useState<ConnectorInfo | null>(null)
  const [catalog, setCatalog] = useState<Scope[]>([])
  const [assistants, setAssistants] = useState<Assistant[]>([])
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [showCreate, setShowCreate] = useState(false)
  const [label, setLabel] = useState('')
  const [provider, setProvider] = useState('')
  const [modelId, setModelId] = useState('')
  const [notes, setNotes] = useState('')
  const [selectedScopes, setSelectedScopes] = useState<Set<string>>(new Set())
  const [reveal, setReveal] = useState<AssistantCreated | null>(null)

  const load = async () => {
    setErr(null)
    try {
      const [ci, cat, list] = await Promise.all([
        api<ConnectorInfo>('/ai-assistants/connector-info'),
        api<Scope[]>('/ai-assistants/scope-catalog'),
        api<Assistant[]>('/ai-assistants'),
      ])
      setConnector(ci)
      setCatalog(cat)
      setAssistants(list)
      // Default: every non-danger scope on. Mirrors the server's
      // DEFAULT_SCOPES so the picker matches the create-with-no-scope
      // case visually.
      if (selectedScopes.size === 0) {
        setSelectedScopes(
          new Set(cat.filter((s) => s.category !== 'danger').map((s) => s.key)),
        )
      }
    } catch (e) {
      setErr((e as Error).message)
    }
  }
  useEffect(() => {
    void load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  async function onCreate(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setErr(null)
    try {
      const res = await api<AssistantCreated>('/ai-assistants', {
        method: 'POST',
        body: JSON.stringify({
          label,
          scope: [...selectedScopes],
          provider: provider || null,
          model_id: modelId || null,
          notes: notes || null,
        }),
      })
      setReveal(res)
      setShowCreate(false)
      setLabel('')
      setProvider('')
      setModelId('')
      setNotes('')
      await load()
    } catch (e) {
      setErr((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  async function onDelete(a: Assistant) {
    if (!window.confirm(t('aiAssistants.deleteConfirm', { label: a.label }))) return
    setErr(null)
    try {
      await authFetch(`/ai-assistants/${a.id}`, { method: 'DELETE' })
      await load()
    } catch (e) {
      setErr((e as Error).message)
    }
  }

  async function onRotate(a: Assistant) {
    if (!window.confirm(t('aiAssistants.rotateConfirm', { label: a.label }))) return
    setErr(null)
    try {
      const res = await api<AssistantCreated>(`/ai-assistants/${a.id}/rotate`, {
        method: 'POST',
      })
      setReveal(res)
      await load()
    } catch (e) {
      setErr((e as Error).message)
    }
  }

  function toggleScope(key: string) {
    setSelectedScopes((s) => {
      const n = new Set(s)
      if (n.has(key)) n.delete(key)
      else n.add(key)
      return n
    })
  }

  function copy(text: string) {
    void navigator.clipboard?.writeText(text)
  }

  const grouped: Record<Scope['category'], Scope[]> = { read: [], write: [], danger: [] }
  for (const s of catalog) grouped[s.category].push(s)

  return (
    <section className="card card--wide">
      <h2>{t('aiAssistants.pageTitle')}</h2>
      <p className="hint">{t('aiAssistants.intro')}</p>
      {connector && (
        <div className="aiconnector">
          <strong>{t('aiAssistants.connectorTitle')}</strong>
          <div className="row">
            <code className="aiconnector__url">
              {window.location.origin}{connector.mcp_url}
            </code>
            <button
              type="button"
              className="btn--ghost btn--sm"
              onClick={() => copy(`${window.location.origin}${connector.mcp_url}`)}
            >
              {t('aiAssistants.copy')}
            </button>
          </div>
          <p className="hint">{t('aiAssistants.transportPending')}</p>
        </div>
      )}
      {err && <p className="err">{err}</p>}
      {reveal && (
        <div className="airreveal">
          <h3>{t('aiAssistants.revealTitle')}</h3>
          <p className="err">
            <strong>{t('aiAssistants.revealWarningTitle')}</strong>{' '}
            {t('aiAssistants.revealWarningBody')}
          </p>
          <div className="row">
            <label>
              {t('aiAssistants.revealUrl')}{' '}
              <code>{window.location.origin}{connector?.mcp_url}</code>
            </label>
            <button
              type="button"
              className="btn--ghost btn--sm"
              onClick={() => copy(`${window.location.origin}${connector?.mcp_url ?? ''}`)}
            >
              {t('aiAssistants.copy')}
            </button>
          </div>
          <div className="row">
            <label>
              {t('aiAssistants.revealSecret')}{' '}
              <code className="aiconnector__url">{reveal.raw_secret}</code>
            </label>
            <button
              type="button"
              className="btn--ghost btn--sm"
              onClick={() => copy(reveal.raw_secret)}
            >
              {t('aiAssistants.copy')}
            </button>
          </div>
          <button
            type="button"
            className="btn"
            onClick={() => setReveal(null)}
          >
            {t('aiAssistants.revealAcknowledge')}
          </button>
        </div>
      )}
      <div className="row">
        {!showCreate && (
          <button type="button" className="btn" onClick={() => setShowCreate(true)}>
            + {t('aiAssistants.newAssistant')}
          </button>
        )}
      </div>
      {showCreate && (
        <form onSubmit={(e) => void onCreate(e)} className="cpform">
          <label>
            {t('aiAssistants.labelLabel')}
            <input
              required
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              minLength={1}
              maxLength={255}
            />
          </label>
          <label>
            {t('aiAssistants.providerLabel')}
            <input
              value={provider}
              onChange={(e) => setProvider(e.target.value)}
              placeholder={t('aiAssistants.providerPlaceholder')}
              maxLength={64}
            />
          </label>
          <label>
            {t('aiAssistants.modelIdLabel')}
            <input
              value={modelId}
              onChange={(e) => setModelId(e.target.value)}
              placeholder={t('aiAssistants.modelIdPlaceholder')}
              maxLength={128}
            />
          </label>
          <label>
            {t('aiAssistants.notesLabel')}
            <textarea
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              maxLength={2000}
              rows={2}
            />
          </label>
          <fieldset className="aiscopes">
            <legend>
              {t('aiAssistants.permissions')} ({selectedScopes.size}/{catalog.length})
            </legend>
            {(['read', 'write', 'danger'] as const).map((cat) => (
              <div key={cat} className="aiscopes__group">
                <strong>{t(`aiAssistants.scopeCategory_${cat}`)}</strong>
                {grouped[cat].map((s) => (
                  <label key={s.key} className="aiscopes__item" title={s.description}>
                    <input
                      type="checkbox"
                      checked={selectedScopes.has(s.key)}
                      onChange={() => toggleScope(s.key)}
                    />
                    <span>
                      <code>{s.key}</code> — {s.label}
                    </span>
                  </label>
                ))}
              </div>
            ))}
          </fieldset>
          <div className="row">
            <button type="submit" className="btn" disabled={busy}>
              {busy ? t('aiAssistants.creating') : t('aiAssistants.create')}
            </button>
            <button
              type="button"
              className="btn--ghost"
              onClick={() => setShowCreate(false)}
              disabled={busy}
            >
              {t('aiAssistants.cancel')}
            </button>
          </div>
        </form>
      )}
      {assistants.length === 0 && !showCreate && (
        <p className="hint">{t('aiAssistants.noAssistants')}</p>
      )}
      <ul className="list">
        {assistants.map((a) => (
          <li key={a.id} className="airow">
            <div className="airow__head">
              <strong>{a.label}</strong>
              <span className="muted">
                {a.provider}
                {a.provider && a.model_id ? ' · ' : ''}
                {a.model_id}
              </span>
              <span
                className={a.is_active ? 'tag tag--ok' : 'tag tag--muted'}
                title={a.token_prefix ?? ''}
              >
                {a.is_active ? t('aiAssistants.statusActive') : t('aiAssistants.statusRevoked')}
              </span>
              <span className="grow" />
              <button
                type="button"
                className="btn--ghost btn--sm"
                onClick={() => void onRotate(a)}
              >
                {t('aiAssistants.rotateSecret')}
              </button>
              <button
                type="button"
                className="btn--ghost btn--sm btn--danger"
                onClick={() => void onDelete(a)}
              >
                {t('aiAssistants.deleteAssistant')}
              </button>
            </div>
            <div className="airow__meta muted">
              <code>{a.token_prefix ?? '—'}…</code> ·{' '}
              {t('aiAssistants.scopesSelected', {
                selected: a.scope.length,
                total: catalog.length,
              })}
            </div>
          </li>
        ))}
      </ul>
    </section>
  )
}
