import { type CSSProperties, type FormEvent, useCallback, useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { authFetch } from '../api/client'

// Ported from bitvision_phoenix/frontend/src/app/settings/ai-assistants
// (Mycelium API shape: scope is a flat ``string[]`` named ``scope``, not
// ``permissions``; Mycelium has no ``deidentify_on_use`` or shared-patient
// surface — those belonged to the medical domain). Categorized scope
// grid (read / write / danger), dangerous-confirm modal, select-all
// with confirm when danger entries exist, clear-all, per-assistant
// edit / rotate-secret / revoke (is_active toggle) / delete, secret
// shown ONCE on create / rotate.

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

const CATEGORY_ORDER: Array<Scope['category']> = ['read', 'write', 'danger']

async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await authFetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init.headers ?? {}) },
  })
  if (!res.ok) {
    let detail: string
    try {
      const j = (await res.json()) as unknown
      detail =
        typeof j === 'object' && j !== null && 'detail' in j
          ? String((j as { detail: unknown }).detail)
          : JSON.stringify(j)
    } catch {
      detail = await res.text().catch(() => '')
    }
    throw new Error(detail || `HTTP ${res.status}`)
  }
  if (res.status === 204) return undefined as T
  return res.json() as Promise<T>
}

const aiApi = {
  list: () => api<Assistant[]>('/ai-assistants'),
  connectorInfo: () => api<ConnectorInfo>('/ai-assistants/connector-info'),
  scopeCatalog: () => api<Scope[]>('/ai-assistants/scope-catalog'),
  create: (body: object) =>
    api<AssistantCreated>('/ai-assistants', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  update: (id: string, body: object) =>
    api<Assistant>(`/ai-assistants/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(body),
    }),
  remove: (id: string) =>
    api<void>(`/ai-assistants/${id}`, { method: 'DELETE' }),
  rotate: (id: string) =>
    api<AssistantCreated>(`/ai-assistants/${id}/rotate`, { method: 'POST' }),
}

function categoryColor(cat: Scope['category']): string {
  return cat === 'danger' ? '#c0392b' : cat === 'write' ? '#d68910' : '#28a745'
}

export function AiAssistantsSettings() {
  const { t } = useTranslation()
  const [assistants, setAssistants] = useState<Assistant[] | null>(null)
  const [connector, setConnector] = useState<ConnectorInfo | null>(null)
  const [copied, setCopied] = useState(false)
  const [reveal, setReveal] = useState<AssistantCreated | null>(null)
  const [creating, setCreating] = useState(false)
  const [editing, setEditing] = useState<Assistant | null>(null)
  const [err, setErr] = useState<string | null>(null)

  const reload = useCallback(async () => {
    try {
      setAssistants(await aiApi.list())
    } catch (e) {
      setErr((e as Error).message)
    }
  }, [])

  useEffect(() => {
    // setState happens inside ``reload`` and the connector promise,
    // both after an await — microtask boundary, no synchronous
    // cascade. react-hooks/set-state-in-effect flags this anyway.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void reload()
    aiApi
      .connectorInfo()
      // eslint-disable-next-line react-hooks/set-state-in-effect
      .then(setConnector)
      .catch(() => {
        /* non-fatal */
      })
  }, [reload])

  const mcpAbsoluteUrl = useMemo(() => {
    if (!connector) return ''
    const u = connector.mcp_url
    return u.startsWith('http') ? u : `${window.location.origin}${u}`
  }, [connector])

  const copyMcpUrl = useCallback(async () => {
    if (!connector) return
    try {
      await navigator.clipboard.writeText(mcpAbsoluteUrl)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 2000)
    } catch {
      /* older browsers */
    }
  }, [connector, mcpAbsoluteUrl])

  const handleDelete = useCallback(
    async (a: Assistant) => {
      if (!window.confirm(t('aiAssistants.deleteConfirm', { label: a.label }))) return
      try {
        await aiApi.remove(a.id)
        await reload()
      } catch (e) {
        setErr((e as Error).message)
      }
    },
    [reload, t],
  )

  const handleSetActive = useCallback(
    async (a: Assistant, next: boolean) => {
      if (!next) {
        if (!window.confirm(t('aiAssistants.revokeConfirm', { label: a.label }))) return
      }
      try {
        await aiApi.update(a.id, {
          expected_version: a.version,
          is_active: next,
        })
        await reload()
      } catch (e) {
        setErr((e as Error).message)
      }
    },
    [reload, t],
  )

  const handleRotate = useCallback(
    async (a: Assistant) => {
      if (!window.confirm(t('aiAssistants.rotateConfirm', { label: a.label }))) return
      try {
        const created = await aiApi.rotate(a.id)
        setReveal(created)
        await reload()
      } catch (e) {
        setErr((e as Error).message)
      }
    },
    [reload, t],
  )

  return (
    <section className="card card--wide aiset">
      <h2>{t('aiAssistants.pageTitle')}</h2>
      <p className="hint">{t('aiAssistants.intro')}</p>

      {err && (
        <p className="err">
          {err}{' '}
          <button type="button" className="btn--ghost btn--sm" onClick={() => setErr(null)}>
            ✕
          </button>
        </p>
      )}

      {connector && (
        <div className="aiconnector">
          <strong>{t('aiAssistants.connectorTitle')}</strong>
          <div className="row aiconnector__row">
            <code className="aiconnector__url">{mcpAbsoluteUrl}</code>
            <button type="button" className="btn--ghost btn--sm" onClick={() => void copyMcpUrl()}>
              {copied ? t('aiAssistants.copied') : t('aiAssistants.copy')}
            </button>
          </div>
          <pre className="aiconnector__instructions">{connector.instructions_md}</pre>
        </div>
      )}

      {reveal && connector && (
        <CredentialsRevealCard
          assistant={reveal}
          mcpUrl={mcpAbsoluteUrl}
          onClose={() => setReveal(null)}
        />
      )}

      <div className="row">
        {!creating && !editing && (
          <button type="button" className="btn" onClick={() => setCreating(true)}>
            + {t('aiAssistants.newAssistant')}
          </button>
        )}
      </div>

      {creating && (
        <AssistantForm
          mode="create"
          onCancel={() => setCreating(false)}
          onSaved={(created) => {
            setCreating(false)
            if (created) setReveal(created)
            void reload()
          }}
        />
      )}

      {editing && (
        <AssistantForm
          mode="edit"
          assistant={editing}
          onCancel={() => setEditing(null)}
          onSaved={() => {
            setEditing(null)
            void reload()
          }}
        />
      )}

      {assistants === null && !err && <p className="hint">…</p>}
      {assistants !== null && assistants.length === 0 && !creating && (
        <p className="hint">{t('aiAssistants.noAssistants')}</p>
      )}
      {assistants?.map((a) => (
        <AssistantCard
          key={a.id}
          assistant={a}
          onEdit={() => {
            setEditing(a)
            setCreating(false)
          }}
          onRotate={() => void handleRotate(a)}
          onSetActive={(next) => void handleSetActive(a, next)}
          onDelete={() => void handleDelete(a)}
        />
      ))}
    </section>
  )
}

function CredentialsRevealCard({
  assistant,
  mcpUrl,
  onClose,
}: {
  assistant: AssistantCreated
  mcpUrl: string
  onClose: () => void
}) {
  const { t } = useTranslation()
  function copy(text: string) {
    void navigator.clipboard?.writeText(text)
  }
  return (
    <div className="airreveal">
      <h3>{t('aiAssistants.revealTitle')}</h3>
      <p className="err">
        <strong>{t('aiAssistants.revealWarningTitle')}</strong>{' '}
        {t('aiAssistants.revealWarningBody')}
      </p>
      <RevealRow
        label={t('aiAssistants.revealUrl')}
        value={mcpUrl}
        onCopy={() => copy(mcpUrl)}
      />
      <RevealRow
        label={t('aiAssistants.revealClientId')}
        value={assistant.assistant.id}
        onCopy={() => copy(assistant.assistant.id)}
      />
      <RevealRow
        label={t('aiAssistants.revealSecret')}
        value={assistant.raw_secret}
        onCopy={() => copy(assistant.raw_secret)}
        monoBig
      />
      <p className="hint">{t('aiAssistants.revealHowTo')}</p>
      <div className="row">
        <button type="button" className="btn" onClick={onClose}>
          {t('aiAssistants.revealAcknowledge')}
        </button>
      </div>
    </div>
  )
}

function RevealRow({
  label,
  value,
  onCopy,
  monoBig,
}: {
  label: string
  value: string
  onCopy: () => void
  monoBig?: boolean
}) {
  const { t } = useTranslation()
  return (
    <div className="row airreveal__row">
      <span className="muted">{label}</span>
      <code className={'aiconnector__url' + (monoBig ? ' airreveal__secret' : '')}>
        {value}
      </code>
      <button type="button" className="btn--ghost btn--sm" onClick={onCopy}>
        {t('aiAssistants.copy')}
      </button>
    </div>
  )
}

function AssistantCard({
  assistant,
  onEdit,
  onRotate,
  onSetActive,
  onDelete,
}: {
  assistant: Assistant
  onEdit: () => void
  onRotate: () => void
  onSetActive: (next: boolean) => void
  onDelete: () => void
}) {
  const { t } = useTranslation()
  return (
    <div className="airow">
      <div className="airow__head">
        <strong>{assistant.label}</strong>
        {(assistant.provider || assistant.model_id) && (
          <span className="muted">
            {assistant.provider}
            {assistant.provider && assistant.model_id ? ' · ' : ''}
            {assistant.model_id}
          </span>
        )}
        <span
          className={
            'tag ' + (assistant.is_active ? 'tag--ok' : 'tag--muted')
          }
        >
          {assistant.is_active
            ? t('aiAssistants.statusActive')
            : t('aiAssistants.statusRevoked')}
        </span>
        <span className="grow" />
        <button type="button" className="btn--ghost btn--sm" onClick={onEdit}>
          {t('aiAssistants.editAssistant')}
        </button>
        <button type="button" className="btn--ghost btn--sm" onClick={onRotate}>
          {t('aiAssistants.rotateSecret')}
        </button>
        <button
          type="button"
          className="btn--ghost btn--sm"
          onClick={() => onSetActive(!assistant.is_active)}
        >
          {assistant.is_active
            ? t('aiAssistants.revoke')
            : t('aiAssistants.reactivate')}
        </button>
        <button
          type="button"
          className="btn--ghost btn--sm btn--danger"
          onClick={onDelete}
        >
          {t('aiAssistants.deleteAssistant')}
        </button>
      </div>
      <div className="airow__meta muted">
        <code>{assistant.token_prefix ?? '—'}…</code>{' '}
        {t('aiAssistants.scopesCount', { n: assistant.scope.length })}
      </div>
    </div>
  )
}

function AssistantForm({
  mode,
  assistant,
  onCancel,
  onSaved,
}: {
  mode: 'create' | 'edit'
  assistant?: Assistant
  onCancel: () => void
  onSaved: (created: AssistantCreated | null) => void
}) {
  const { t } = useTranslation()
  const [label, setLabel] = useState(assistant?.label ?? '')
  const [provider, setProvider] = useState(assistant?.provider ?? '')
  const [modelId, setModelId] = useState(assistant?.model_id ?? '')
  const [notes, setNotes] = useState(assistant?.notes ?? '')
  const [catalog, setCatalog] = useState<Scope[] | null>(null)
  const [perms, setPerms] = useState<Record<string, boolean>>({})
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    aiApi
      .scopeCatalog()
      .then((entries) => {
        if (cancelled) return
        setCatalog(entries)
        if (mode === 'edit' && assistant) {
          const sel = new Set(assistant.scope)
          setPerms(Object.fromEntries(entries.map((e) => [e.key, sel.has(e.key)])))
        } else {
          // Default-on: every non-danger entry (mirrors backend
          // DEFAULT_SCOPES in core/src/mycelium_core/mcp_scopes.py).
          setPerms(
            Object.fromEntries(entries.map((e) => [e.key, e.category !== 'danger'])),
          )
        }
      })
      .catch(() => {
        if (cancelled) return
        setErr(t('aiAssistants.loadFailed'))
      })
    return () => {
      cancelled = true
    }
  }, [mode, assistant, t])

  const grouped = useMemo(() => {
    if (!catalog) return null
    const out = new Map<Scope['category'], Scope[]>()
    for (const cat of CATEGORY_ORDER) out.set(cat, [])
    for (const e of catalog) out.get(e.category)?.push(e)
    return out
  }, [catalog])

  const selectedCount = useMemo(
    () => Object.values(perms).filter(Boolean).length,
    [perms],
  )

  function togglePermission(entry: Scope, next: boolean) {
    if (next && entry.category === 'danger') {
      if (
        !window.confirm(
          t('aiAssistants.dangerousScopeConfirm', { label: entry.label }),
        )
      )
        return
    }
    setPerms((prev) => ({ ...prev, [entry.key]: next }));
  }
  function selectAll() {
    if (!catalog) return
    const dangerCount = catalog.filter((e) => e.category === 'danger').length
    if (dangerCount > 0) {
      if (!window.confirm(t('aiAssistants.selectAllConfirm', { dangerCount })))
        return
    }
    setPerms(Object.fromEntries(catalog.map((e) => [e.key, true])))
  }
  function clearAll() {
    if (!catalog) return
    setPerms(Object.fromEntries(catalog.map((e) => [e.key, false])))
  }

  async function submit(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setErr(null)
    try {
      const selected = (catalog ?? [])
        .filter((p) => perms[p.key])
        .map((p) => p.key)
      if (selected.length === 0) throw new Error(t('aiAssistants.atLeastOnePerm'))
      const body = {
        label,
        scope: selected,
        provider: provider.trim() || null,
        model_id: modelId.trim() || null,
        notes: notes.trim() || null,
      }
      if (mode === 'create') {
        const created = await aiApi.create(body)
        onSaved(created)
      } else if (assistant) {
        await aiApi.update(assistant.id, {
          ...body,
          expected_version: assistant.version,
        })
        onSaved(null)
      }
    } catch (e) {
      setErr((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="card aiform" onSubmit={(e) => void submit(e)}>
      <h3>
        {mode === 'create'
          ? t('aiAssistants.createTitle')
          : t('aiAssistants.editTitle', { label: assistant?.label ?? '' })}
      </h3>
      {err && <p className="err">{err}</p>}

      <div className="aiform__grid">
        <label className="aiform__full">
          <span className="muted">{t('aiAssistants.labelLabel')}</span>
          <input
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            required
            minLength={1}
            maxLength={255}
          />
        </label>
        <label>
          <span className="muted">{t('aiAssistants.providerLabel')}</span>
          <input
            value={provider}
            onChange={(e) => setProvider(e.target.value)}
            placeholder={t('aiAssistants.providerPlaceholder')}
            maxLength={64}
          />
        </label>
        <label>
          <span className="muted">{t('aiAssistants.modelIdLabel')}</span>
          <input
            value={modelId}
            onChange={(e) => setModelId(e.target.value)}
            placeholder={t('aiAssistants.modelIdPlaceholder')}
            maxLength={128}
          />
        </label>
        <label className="aiform__full">
          <span className="muted">{t('aiAssistants.notesLabel')}</span>
          <textarea
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
            rows={2}
            maxLength={2000}
          />
        </label>
      </div>

      <fieldset className="aiform__perms">
        <legend>
          <span className="aiform__perms-title">
            <strong>{t('aiAssistants.permissions')}</strong>
            {catalog && (
              <span className="muted">
                {t('aiAssistants.scopesSelected', {
                  selected: selectedCount,
                  total: catalog.length,
                })}
              </span>
            )}
          </span>
          {catalog && (
            <span className="aiform__perms-actions">
              <button type="button" className="btn--ghost btn--sm" onClick={() => void selectAll()}>
                {t('aiAssistants.selectAllScopes')}
              </button>
              <button type="button" className="btn--ghost btn--sm" onClick={clearAll}>
                {t('aiAssistants.clearAllScopes')}
              </button>
            </span>
          )}
        </legend>
        {!grouped && <p className="hint">…</p>}
        {grouped &&
          CATEGORY_ORDER.map((cat) => {
            const entries = grouped.get(cat) ?? []
            if (entries.length === 0) return null
            const style: CSSProperties = {
              borderLeft: `3px solid ${categoryColor(cat)}`,
              background: cat === 'danger' ? 'rgba(192,57,43,0.05)' : undefined,
            }
            return (
              <div key={cat} className="aiform__perms-group" style={style}>
                <div className="aiform__perms-cat muted">
                  {t(`aiAssistants.scopeCategory_${cat}`)}
                </div>
                {entries.map((entry) => (
                  <label key={entry.key} className="aiform__perm">
                    <input
                      type="checkbox"
                      checked={!!perms[entry.key]}
                      onChange={(e) => togglePermission(entry, e.target.checked)}
                    />
                    <div>
                      <div>
                        <strong>{entry.label}</strong>{' '}
                        <code className="aiform__perm-key">{entry.key}</code>
                      </div>
                      {entry.description && (
                        <div className="hint">{entry.description}</div>
                      )}
                    </div>
                  </label>
                ))}
              </div>
            )
          })}
      </fieldset>

      <div className="row aiform__actions">
        <button type="button" className="btn--ghost" onClick={onCancel} disabled={busy}>
          {t('aiAssistants.cancel')}
        </button>
        <button type="submit" className="btn" disabled={busy}>
          {busy
            ? mode === 'create'
              ? t('aiAssistants.creating')
              : t('aiAssistants.saving')
            : mode === 'create'
              ? t('aiAssistants.create')
              : t('aiAssistants.saveChanges')}
        </button>
      </div>
    </form>
  )
}
