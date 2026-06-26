import { useEffect, useMemo, useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { authFetch } from '../api/client'

// /agent-tokens endpoints are being added by the v1.1.0 A backend
// agent; types may not yet be in schema.d.ts. We use authFetch + the
// contract shapes documented in the v1.1.0 B prompt.
// TODO("schema regen pending after backend A merges")

type AgentToken = {
  id: string
  name: string
  scope: string
  prefix: string
  expires_at: string | null
  last_used_at: string | null
  revoked_at: string | null
  created_at: string
}

type AgentTokenCreated = AgentToken & { raw: string }

function slugify(s: string): string {
  return s
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 24)
}

function apiBaseUrl(): string {
  return `${window.location.origin}/api`
}

function buildSnippet(name: string, prefix: string, rawToken: string): {
  serverKey: string
  snippet: string
} {
  const short = (prefix || '').replace(/[^a-zA-Z0-9]/g, '').slice(0, 6)
  const slug = slugify(name)
  const serverKey = slug ? `mycelium-${slug}-${short}` : `mycelium-${short}`
  const config = {
    mcpServers: {
      [serverKey]: {
        command: 'uv',
        args: [
          'run',
          '--project',
          '/path/to/mycelium/mcp',
          'python',
          '-m',
          'mycelium_mcp.main',
        ],
        env: {
          MYCELIUM_MCP_BASE_URL: apiBaseUrl(),
          MYCELIUM_MCP_AGENT_TOKEN: rawToken,
        },
      },
    },
  }
  return { serverKey, snippet: JSON.stringify(config, null, 2) }
}

export function McpConnect() {
  const { t } = useTranslation()
  const [rows, setRows] = useState<AgentToken[]>([])
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState<string | null>(null)

  const [showForm, setShowForm] = useState(false)
  const [name, setName] = useState('Claude Desktop')
  const [ttlDays, setTtlDays] = useState<number | ''>(365)

  // The raw token is held in component state only after a successful
  // POST; it is cleared on next mount (component re-render that
  // discards `created`). We never persist it.
  const [created, setCreated] = useState<AgentTokenCreated | null>(null)
  const [copiedToken, setCopiedToken] = useState(false)
  const [copiedSnippet, setCopiedSnippet] = useState(false)
  const [tick, setTick] = useState(0)

  useEffect(() => {
    let active = true
    void (async () => {
      const res = await authFetch('/agent-tokens')
      if (!active) return
      if (!res.ok) {
        setErr(`HTTP ${res.status}`)
        setLoading(false)
        return
      }
      const data = (await res.json()) as AgentToken[]
      setRows((data ?? []).filter((r) => r.scope === 'mcp'))
      setLoading(false)
    })()
    return () => {
      active = false
    }
  }, [tick])

  function reload() {
    setErr(null)
    setTick((n) => n + 1)
  }

  async function onCreate(e: FormEvent) {
    e.preventDefault()
    setErr(null)
    const body: Record<string, unknown> = { name, scope: 'mcp' }
    if (typeof ttlDays === 'number') body.ttl_days = ttlDays
    const res = await authFetch('/agent-tokens', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    if (!res.ok) {
      setErr(`HTTP ${res.status}`)
      return
    }
    const data = (await res.json()) as AgentTokenCreated
    setCreated(data)
    setShowForm(false)
    setName('Claude Desktop')
    setTtlDays(365)
    reload()
  }

  async function onRevoke(id: string) {
    setErr(null)
    const res = await authFetch(`/agent-tokens/${id}`, { method: 'DELETE' })
    if (!res.ok) {
      setErr(`HTTP ${res.status}`)
      return
    }
    reload()
  }

  async function copy(text: string, setFlag: (b: boolean) => void) {
    try {
      await navigator.clipboard.writeText(text)
      setFlag(true)
      window.setTimeout(() => setFlag(false), 2000)
    } catch {
      /* clipboard may be blocked */
    }
  }

  const createdSnippet = useMemo(() => {
    if (!created) return null
    return buildSnippet(created.name, created.prefix, created.raw)
  }, [created])

  return (
    <section className="card">
      <h2>{t('connectors.mcp.title')}</h2>
      {err && <p className="err">{err}</p>}
      {loading && <p>{t('home.loading')}</p>}

      {!loading && rows.length === 0 && !created && (
        <p>{t('connectors.mcp.empty')}</p>
      )}

      {!loading && rows.length > 0 && (
        <ul>
          {rows.map((r) => (
            <li key={r.id}>
              <strong>{r.name}</strong>{' '}
              <code>
                {t('connectors.mcp.prefix')}: {r.prefix}
              </code>{' '}
              <span>
                {t('connectors.mcp.createdAt')}:{' '}
                {new Date(r.created_at).toLocaleString()}
              </span>{' '}
              {r.last_used_at && (
                <span>
                  | {t('connectors.mcp.lastUsedAt')}:{' '}
                  {new Date(r.last_used_at).toLocaleString()}
                </span>
              )}{' '}
              {r.expires_at && (
                <span>
                  | {t('connectors.mcp.expires')}:{' '}
                  {new Date(r.expires_at).toLocaleString()}
                </span>
              )}{' '}
              {r.revoked_at ? (
                <em>({t('connectors.mcp.revoked')})</em>
              ) : (
                <button type="button" onClick={() => void onRevoke(r.id)}>
                  {t('connectors.mcp.revoke')}
                </button>
              )}
            </li>
          ))}
        </ul>
      )}

      {!created && !showForm && (
        <button type="button" onClick={() => setShowForm(true)}>
          {t('connectors.mcp.generate')}
        </button>
      )}

      {showForm && !created && (
        <form onSubmit={(e) => void onCreate(e)}>
          <label>
            {t('connectors.mcp.name')}
            <input
              required
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </label>{' '}
          <label>
            {t('connectors.mcp.ttlDays')}
            <input
              type="number"
              min={1}
              value={ttlDays}
              onChange={(e) =>
                setTtlDays(e.target.value === '' ? '' : Number(e.target.value))
              }
            />
          </label>{' '}
          <button type="submit">{t('connectors.mcp.confirmGenerate')}</button>{' '}
          <button type="button" onClick={() => setShowForm(false)}>
            {t('connectors.mcp.cancel')}
          </button>
        </form>
      )}

      {created && createdSnippet && (
        <div>
          <p className="err">{t('connectors.mcp.tokenWarning')}</p>
          <label>
            <strong>{t('connectors.mcp.token')}</strong>
            <textarea
              readOnly
              value={created.raw}
              rows={2}
              onFocus={(e) => e.currentTarget.select()}
              style={{ width: '100%', fontFamily: 'monospace' }}
            />
          </label>
          <button
            type="button"
            onClick={() => void copy(created.raw, setCopiedToken)}
          >
            {copiedToken ? 'OK' : t('connectors.mcp.copyToken')}
          </button>{' '}
          <button
            type="button"
            onClick={() => void copy(createdSnippet.snippet, setCopiedSnippet)}
          >
            {copiedSnippet ? 'OK' : t('connectors.mcp.copySnippet')}
          </button>
          <pre
            style={{
              background: 'var(--surface-2)',
              padding: '0.6rem',
              borderRadius: 4,
              overflow: 'auto',
              fontSize: '0.8rem',
              maxHeight: 320,
            }}
          >
            {createdSnippet.snippet}
          </pre>
          <p>{t('connectors.mcp.configHint')}</p>
          <button type="button" onClick={() => setCreated(null)}>
            {t('connectors.mcp.dismiss')}
          </button>
        </div>
      )}
    </section>
  )
}
