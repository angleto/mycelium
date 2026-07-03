import { useEffect, useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import type { components } from '../api/schema'

type ApiKey = components['schemas']['IssuerApiKeyOut']
// The mint / rotate response is the only place the plaintext secret appears.
type ApiKeyCreated = components['schemas']['IssuerApiKeyCreateOut']

// The permission vocabulary (mirrors the service whitelist). Shown as-is: the
// raw strings are unambiguous for an integrator and avoid an i18n key carrying
// the ``:`` separator.
const PERMISSIONS = [
  'invoice:read',
  'invoice:compose',
  'invoice:send',
  'invoice:credit_note',
  'invoice:download',
  'invoice:client_write',
] as const

// Split a comma/space-separated CIDR list; empty -> null (unrestricted).
function parseAllowlist(text: string): string[] | null {
  const entries = text
    .split(/[\s,]+/)
    .map((s) => s.trim())
    .filter(Boolean)
  return entries.length > 0 ? entries : null
}

export function IssuerApiKeys({ profileId }: { profileId: string }) {
  const { t } = useTranslation()
  const [rows, setRows] = useState<ApiKey[]>([])
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState<string | null>(null)
  const [tick, setTick] = useState(0)

  const [showForm, setShowForm] = useState(false)
  const [name, setName] = useState('')
  const [perms, setPerms] = useState<string[]>(['invoice:read'])
  const [ttlDays, setTtlDays] = useState<number | ''>('')
  // Comma-separated CIDR blocks; empty = unrestricted.
  const [allowlist, setAllowlist] = useState('')

  // Held in state only after a successful mint/rotate; never persisted, cleared
  // on dismiss / remount.
  const [created, setCreated] = useState<ApiKeyCreated | null>(null)
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    let active = true
    void (async () => {
      const { data, error } = await api.GET(
        '/issuer-profiles/{issuer_profile_id}/api-keys',
        { params: { header: workspaceHeader(), path: { issuer_profile_id: profileId } } },
      )
      if (!active) return
      if (error) {
        setErr(errMessage(error))
        setLoading(false)
        return
      }
      setRows(data ?? [])
      setLoading(false)
    })()
    return () => {
      active = false
    }
  }, [profileId, tick])

  function reload() {
    setErr(null)
    setTick((n) => n + 1)
  }

  function togglePerm(p: string) {
    setPerms((cur) => (cur.includes(p) ? cur.filter((x) => x !== p) : [...cur, p]))
  }

  async function onMint(e: FormEvent) {
    e.preventDefault()
    setErr(null)
    const body: components['schemas']['IssuerApiKeyCreateIn'] = {
      name,
      permissions: perms,
      ttl_days: typeof ttlDays === 'number' ? ttlDays : null,
      ip_allowlist: parseAllowlist(allowlist),
    }
    const { data, error } = await api.POST(
      '/issuer-profiles/{issuer_profile_id}/api-keys',
      { params: { header: workspaceHeader(), path: { issuer_profile_id: profileId } }, body },
    )
    if (error) {
      setErr(errMessage(error))
      return
    }
    setCreated(data ?? null)
    setShowForm(false)
    setName('')
    setPerms(['invoice:read'])
    setTtlDays('')
    setAllowlist('')
    reload()
  }

  async function onRotate(keyId: string) {
    if (!window.confirm(t('issuerApiKeys.rotateConfirm'))) return
    setErr(null)
    const { data, error } = await api.POST(
      '/issuer-profiles/{issuer_profile_id}/api-keys/{key_id}/rotate',
      {
        params: {
          header: workspaceHeader(),
          path: { issuer_profile_id: profileId, key_id: keyId },
        },
      },
    )
    if (error) {
      setErr(errMessage(error))
      return
    }
    setCreated(data ?? null)
    reload()
  }

  async function onEditAllowlist(k: ApiKey) {
    const current = (k.ip_allowlist ?? []).join(', ')
    const input = window.prompt(t('issuerApiKeys.allowlistPrompt'), current)
    if (input === null) return
    setErr(null)
    const { error } = await api.PUT(
      '/issuer-profiles/{issuer_profile_id}/api-keys/{key_id}/allowlist',
      {
        params: {
          header: workspaceHeader(),
          path: { issuer_profile_id: profileId, key_id: k.id },
        },
        body: { ip_allowlist: parseAllowlist(input) },
      },
    )
    if (error) {
      setErr(errMessage(error))
      return
    }
    reload()
  }

  async function onRevoke(keyId: string) {
    if (!window.confirm(t('issuerApiKeys.revokeConfirm'))) return
    setErr(null)
    const { error } = await api.DELETE(
      '/issuer-profiles/{issuer_profile_id}/api-keys/{key_id}',
      {
        params: {
          header: workspaceHeader(),
          path: { issuer_profile_id: profileId, key_id: keyId },
        },
      },
    )
    if (error) {
      setErr(errMessage(error))
      return
    }
    reload()
  }

  // Hard-delete an ALREADY-revoked key so the dead row leaves the list.
  async function onPurge(keyId: string) {
    if (!window.confirm(t('issuerApiKeys.purgeConfirm'))) return
    setErr(null)
    const { error } = await api.DELETE(
      '/issuer-profiles/{issuer_profile_id}/api-keys/{key_id}',
      {
        params: {
          header: workspaceHeader(),
          path: { issuer_profile_id: profileId, key_id: keyId },
          query: { hard: true },
        },
      },
    )
    if (error) {
      setErr(errMessage(error))
      return
    }
    reload()
  }

  async function copy(text: string) {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 2000)
    } catch {
      /* clipboard may be blocked */
    }
  }

  return (
    <div className="card card--running">
      <h3>{t('issuerApiKeys.title')}</h3>
      <p className="hint">{t('issuerApiKeys.hint')}</p>
      {err && <p className="err">{err}</p>}
      {loading && <p>{t('home.loading')}</p>}

      {created && (
        <div className="field">
          <p className="err">{t('issuerApiKeys.secretWarning')}</p>
          <textarea
            readOnly
            value={created.raw}
            rows={2}
            onFocus={(e) => e.currentTarget.select()}
            style={{ width: '100%', fontFamily: 'monospace' }}
          />
          <div className="row">
            <button type="button" className="btn--sm" onClick={() => void copy(created.raw)}>
              {copied ? 'OK' : t('issuerApiKeys.copy')}
            </button>
            <button type="button" className="btn--sm btn--ghost" onClick={() => setCreated(null)}>
              {t('issuerApiKeys.dismiss')}
            </button>
          </div>
        </div>
      )}

      {!loading && rows.length === 0 && !created && <p className="muted">{t('issuerApiKeys.empty')}</p>}

      {rows.length > 0 && (
        <ul className="list">
          {rows.map((k) => (
            <li key={k.id}>
              <strong>{k.name}</strong> <code>{k.prefix}</code>{' '}
              <span className="muted">{k.permissions.join(', ')}</span>{' '}
              <span>
                {t('issuerApiKeys.expiresAt')}: {new Date(k.expires_at).toLocaleDateString()} (
                {k.days_to_expiry}
                {t('issuerApiKeys.daysSuffix')})
              </span>{' '}
              {k.last_used_at && (
                <span className="muted">
                  | {t('issuerApiKeys.lastUsedAt')}: {new Date(k.last_used_at).toLocaleString()}
                </span>
              )}{' '}
              {k.ip_allowlist && k.ip_allowlist.length > 0 && (
                <span className="muted">
                  | {t('issuerApiKeys.allowlist')}: <code>{k.ip_allowlist.join(', ')}</code>
                </span>
              )}{' '}
              {k.revoked_at ? (
                <>
                  <em>({t('issuerApiKeys.revoked')})</em>{' '}
                  <button
                    type="button"
                    className="btn--sm btn--danger"
                    onClick={() => void onPurge(k.id)}
                  >
                    {t('issuerApiKeys.delete')}
                  </button>
                </>
              ) : (
                <>
                  <button type="button" className="btn--sm" onClick={() => void onEditAllowlist(k)}>
                    {t('issuerApiKeys.editAllowlist')}
                  </button>{' '}
                  <button type="button" className="btn--sm" onClick={() => void onRotate(k.id)}>
                    {t('issuerApiKeys.rotate')}
                  </button>{' '}
                  <button
                    type="button"
                    className="btn--sm btn--danger"
                    onClick={() => void onRevoke(k.id)}
                  >
                    {t('issuerApiKeys.revoke')}
                  </button>
                </>
              )}
            </li>
          ))}
        </ul>
      )}

      {!showForm && (
        <button type="button" className="btn--sm" onClick={() => setShowForm(true)}>
          {t('issuerApiKeys.mint')}
        </button>
      )}

      {showForm && (
        <form onSubmit={(e) => void onMint(e)}>
          <label>
            {t('issuerApiKeys.name')}
            <input required value={name} onChange={(e) => setName(e.target.value)} />
          </label>
          <div className="field">
            <span>{t('issuerApiKeys.permissions')}</span>
            {PERMISSIONS.map((p) => (
              <label key={p} className="row">
                <input type="checkbox" checked={perms.includes(p)} onChange={() => togglePerm(p)} />
                <code>{p}</code>
              </label>
            ))}
          </div>
          <label>
            {t('issuerApiKeys.ttlDays')}
            <input
              type="number"
              min={1}
              max={365}
              value={ttlDays}
              onChange={(e) => setTtlDays(e.target.value === '' ? '' : Number(e.target.value))}
            />
          </label>
          <label>
            {t('issuerApiKeys.allowlist')}
            <input
              value={allowlist}
              placeholder="203.0.113.0/24, 198.51.100.7"
              onChange={(e) => setAllowlist(e.target.value)}
            />
          </label>
          <p className="hint">{t('issuerApiKeys.allowlistHint')}</p>{' '}
          <button type="submit" className="btn--sm">
            {t('issuerApiKeys.confirmMint')}
          </button>{' '}
          <button
            type="button"
            className="btn--sm btn--ghost"
            onClick={() => setShowForm(false)}
          >
            {t('issuerApiKeys.cancel')}
          </button>
        </form>
      )}
    </div>
  )
}
