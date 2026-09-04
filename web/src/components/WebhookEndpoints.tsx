import { useEffect, useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import type { components } from '../shared'

type Endpoint = components['schemas']['WebhookEndpointOut']
// The create / rotate response is the only place the signing secret appears.
type EndpointCreated = components['schemas']['WebhookEndpointCreateOut']

// The event vocabulary (mirrors the service). Raw strings: unambiguous for an
// integrator and they avoid an i18n key carrying the ``.`` separator.
const EVENT_TYPES = [
  'invoice.transmitted',
  'invoice.delivered',
  'invoice.accepted',
  'invoice.rejected',
  'invoice.deemed_accepted',
  'invoice.payment_recorded',
] as const

export function WebhookEndpoints({ profileId }: { profileId: string }) {
  const { t } = useTranslation()
  const [rows, setRows] = useState<Endpoint[]>([])
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState<string | null>(null)
  const [tick, setTick] = useState(0)

  const [showForm, setShowForm] = useState(false)
  const [name, setName] = useState('')
  const [url, setUrl] = useState('')
  // Empty = subscribe to all events.
  const [events, setEvents] = useState<string[]>([])

  const [created, setCreated] = useState<EndpointCreated | null>(null)
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    let active = true
    void (async () => {
      const { data, error } = await api.GET(
        '/issuer-profiles/{issuer_profile_id}/webhook-endpoints',
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

  function toggleEvent(ev: string) {
    setEvents((cur) => (cur.includes(ev) ? cur.filter((x) => x !== ev) : [...cur, ev]))
  }

  async function onCreate(e: FormEvent) {
    e.preventDefault()
    setErr(null)
    const { data, error } = await api.POST(
      '/issuer-profiles/{issuer_profile_id}/webhook-endpoints',
      {
        params: { header: workspaceHeader(), path: { issuer_profile_id: profileId } },
        body: { name, url, event_types: events },
      },
    )
    if (error) {
      setErr(errMessage(error))
      return
    }
    setCreated(data ?? null)
    setShowForm(false)
    setName('')
    setUrl('')
    setEvents([])
    reload()
  }

  async function onRotate(id: string) {
    if (!window.confirm(t('webhooks.rotateConfirm'))) return
    setErr(null)
    const { data, error } = await api.POST(
      '/issuer-profiles/{issuer_profile_id}/webhook-endpoints/{endpoint_id}/rotate-secret',
      {
        params: {
          header: workspaceHeader(),
          path: { issuer_profile_id: profileId, endpoint_id: id },
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

  async function onRevoke(id: string) {
    if (!window.confirm(t('webhooks.revokeConfirm'))) return
    setErr(null)
    const { error } = await api.DELETE(
      '/issuer-profiles/{issuer_profile_id}/webhook-endpoints/{endpoint_id}',
      {
        params: {
          header: workspaceHeader(),
          path: { issuer_profile_id: profileId, endpoint_id: id },
        },
      },
    )
    if (error) {
      setErr(errMessage(error))
      return
    }
    reload()
  }

  // Hard-delete an ALREADY-revoked endpoint so the dead row leaves the list.
  async function onPurge(id: string) {
    if (!window.confirm(t('webhooks.purgeConfirm'))) return
    setErr(null)
    const { error } = await api.DELETE(
      '/issuer-profiles/{issuer_profile_id}/webhook-endpoints/{endpoint_id}',
      {
        params: {
          header: workspaceHeader(),
          path: { issuer_profile_id: profileId, endpoint_id: id },
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
      <h3>{t('webhooks.title')}</h3>
      <p className="hint">{t('webhooks.hint')}</p>
      {err && <p className="err">{err}</p>}
      {loading && <p>{t('home.loading')}</p>}

      {created && (
        <div className="field">
          <p className="err">{t('webhooks.secretWarning')}</p>
          <textarea
            readOnly
            value={created.secret}
            rows={2}
            onFocus={(e) => e.currentTarget.select()}
            style={{ width: '100%', fontFamily: 'monospace' }}
          />
          <div className="row">
            <button type="button" className="btn--sm" onClick={() => void copy(created.secret)}>
              {copied ? 'OK' : t('webhooks.copy')}
            </button>
            <button type="button" className="btn--sm btn--ghost" onClick={() => setCreated(null)}>
              {t('webhooks.dismiss')}
            </button>
          </div>
        </div>
      )}

      {!loading && rows.length === 0 && !created && (
        <p className="muted">{t('webhooks.empty')}</p>
      )}

      {rows.length > 0 && (
        <ul className="list">
          {rows.map((e) => (
            <li key={e.id}>
              <strong>{e.name}</strong> <code>{e.url}</code>{' '}
              <span className="muted">
                {e.event_types.length > 0 ? e.event_types.join(', ') : t('webhooks.allEvents')}
              </span>{' '}
              {e.revoked_at ? (
                <>
                  <em>({t('webhooks.revoked')})</em>{' '}
                  <button
                    type="button"
                    className="btn--sm btn--danger"
                    onClick={() => void onPurge(e.id)}
                  >
                    {t('webhooks.delete')}
                  </button>
                </>
              ) : (
                <>
                  <button type="button" className="btn--sm" onClick={() => void onRotate(e.id)}>
                    {t('webhooks.rotate')}
                  </button>{' '}
                  <button
                    type="button"
                    className="btn--sm btn--danger"
                    onClick={() => void onRevoke(e.id)}
                  >
                    {t('webhooks.revoke')}
                  </button>
                </>
              )}
            </li>
          ))}
        </ul>
      )}

      {!showForm && (
        <button type="button" className="btn--sm" onClick={() => setShowForm(true)}>
          {t('webhooks.add')}
        </button>
      )}

      {showForm && (
        <form onSubmit={(e) => void onCreate(e)}>
          <label>
            {t('webhooks.name')}
            <input required value={name} onChange={(e) => setName(e.target.value)} />
          </label>
          <label>
            {t('webhooks.url')}
            <input
              required
              type="url"
              value={url}
              placeholder="https://example.com/hooks/mycelium"
              onChange={(e) => setUrl(e.target.value)}
            />
          </label>
          <div className="field">
            <span>{t('webhooks.events')}</span>
            <p className="hint">{t('webhooks.eventsHint')}</p>
            {EVENT_TYPES.map((ev) => (
              <label key={ev} className="row">
                <input
                  type="checkbox"
                  checked={events.includes(ev)}
                  onChange={() => toggleEvent(ev)}
                />
                <code>{ev}</code>
              </label>
            ))}
          </div>
          <button type="submit" className="btn--sm">
            {t('webhooks.confirmAdd')}
          </button>{' '}
          <button
            type="button"
            className="btn--sm btn--ghost"
            onClick={() => setShowForm(false)}
          >
            {t('webhooks.cancel')}
          </button>
        </form>
      )}
    </div>
  )
}
