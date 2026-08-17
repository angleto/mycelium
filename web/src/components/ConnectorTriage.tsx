// Operational triage for inbound payment connectors, mounted with the
// DOCUMENTS rather than with the configuration.
//
// The event list and the delivery ledger answer "what did the connector do to
// my invoices, and what did it turn away?". That is daily work on fiscal
// documents, not a setting, and it was previously reachable only from
// Settings -> issuer profile -> Payment connectors, which is where you go once
// to configure and never again.
//
// The two panels are the SAME components the settings card used, imported
// rather than reimplemented: same requests, same actions, same markup. This
// module only supplies what the settings card supplied implicitly -- which
// connector we are looking at -- because /invoices is not scoped to an issuer
// profile.
import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { authFetch } from '../api/client'
import { ConnectorDeliveries, ConnectorEvents } from './PaymentConnectors'

type Profile = { id: string; label: string }
type Connector = {
  id: string
  label: string
  provider: string
  issuer_profile_id: string
  enabled: boolean
}

/** A connector plus the issuer it belongs to: the pair the panels need. */
type Entry = { connector: Connector; profile: Profile }

export function ConnectorTriage({ profiles }: { profiles: Profile[] }) {
  const { t } = useTranslation()
  const [entries, setEntries] = useState<Entry[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    async function load() {
      setLoading(true)
      setErr(null)
      try {
        // One request per issuer profile, reusing the existing nested route
        // rather than adding an org-wide one: profiles are few (usually one),
        // and not touching the backend keeps this move regression-free.
        const found: Entry[] = []
        for (const profile of profiles) {
          const res = await authFetch(
            `/issuer-profiles/${profile.id}/payment-connectors`,
          )
          if (!res.ok) throw new Error(String(res.status))
          const list = (await res.json()) as Connector[]
          for (const c of list) found.push({ connector: c, profile })
        }
        if (cancelled) return
        setEntries(found)
        setSelected((cur) => cur ?? found[0]?.connector.id ?? null)
      } catch {
        if (!cancelled) setErr(t('paymentConnectors.triageLoadError'))
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    void load()
    return () => {
      cancelled = true
    }
  }, [profiles, t])

  const current = entries.find((e) => e.connector.id === selected) ?? null

  return (
    <div className="card card--wide">
      <h3>{t('paymentConnectors.triageTitle')}</h3>
      <p className="hint">{t('paymentConnectors.triageHint')}</p>
      {err && <p className="err">{err}</p>}
      {loading && <p>{t('home.loading')}</p>}
      {!loading && entries.length === 0 && (
        <p className="hint">{t('paymentConnectors.triageNone')}</p>
      )}
      {entries.length > 1 && (
        <label className="row">
          {t('paymentConnectors.triagePick')}
          <select value={selected ?? ''} onChange={(e) => setSelected(e.target.value)}>
            {entries.map((e) => (
              <option key={e.connector.id} value={e.connector.id}>
                {e.profile.label} · {e.connector.label} ({e.connector.provider})
              </option>
            ))}
          </select>
        </label>
      )}
      {current && (
        <>
          <ConnectorEvents
            profileId={current.profile.id}
            connectorId={current.connector.id}
          />
          <ConnectorDeliveries
            profileId={current.profile.id}
            connectorId={current.connector.id}
          />
        </>
      )}
    </div>
  )
}
