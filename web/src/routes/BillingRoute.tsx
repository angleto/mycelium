import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import { useSession } from '../auth/useSession'
import type { components } from '../api/schema'

type Ledger = components['schemas']['LedgerOut']
type Rate = components['schemas']['RateCardOut']
type Usage = components['schemas']['UsageOut']

export function BillingRoute() {
  const { t } = useTranslation()
  const session = useSession()
  const activeId = session?.workspaceId
  const [balance, setBalance] = useState<string>('')
  const [ledger, setLedger] = useState<Ledger[]>([])
  const [rates, setRates] = useState<Rate[]>([])
  const [usage, setUsage] = useState<Usage[]>([])
  const [amount, setAmount] = useState(0)
  const [reason, setReason] = useState('')
  const [err, setErr] = useState<string | null>(null)

  const reload = useCallback(async () => {
    const h = workspaceHeader()
    const [b, l, r, u] = await Promise.all([
      api.GET('/billing/balance', { params: { header: h } }),
      api.GET('/billing/ledger', { params: { header: h } }),
      api.GET('/billing/rate-cards', { params: { header: h } }),
      api.GET('/billing/usage', { params: { header: h } }),
    ])
    if (b.data) setBalance(b.data.balance)
    if (l.data) setLedger(l.data)
    if (r.data) setRates(r.data)
    if (u.data) setUsage(u.data)
  }, [])

  useEffect(() => {
    let active = true
    void (async () => {
      const h = workspaceHeader()
      const [b, l, r, u] = await Promise.all([
        api.GET('/billing/balance', { params: { header: h } }),
        api.GET('/billing/ledger', { params: { header: h } }),
        api.GET('/billing/rate-cards', { params: { header: h } }),
        api.GET('/billing/usage', { params: { header: h } }),
      ])
      if (!active) return
      if (b.data) setBalance(b.data.balance)
      if (l.data) setLedger(l.data)
      if (r.data) setRates(r.data)
      if (u.data) setUsage(u.data)
    })()
    return () => {
      active = false
    }
  }, [activeId])

  async function onGrant(e: FormEvent) {
    e.preventDefault()
    setErr(null)
    const { error } = await api.POST('/billing/grant', {
      params: { header: workspaceHeader() },
      body: { amount, reason: reason || null },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    setAmount(0)
    setReason('')
    await reload()
  }

  return (
    <section className="card">
      <h1>{t('billing.title')}</h1>
      {err && <p className="err">{err}</p>}
      <p>
        <strong>{t('billing.balance')}:</strong> {balance || '0'}
      </p>

      <form onSubmit={(e) => void onGrant(e)} className="row">
        <input
          type="number"
          required
          placeholder={t('billing.amount')}
          value={amount}
          onChange={(e) => setAmount(Number(e.target.value))}
        />
        <input
          placeholder={t('billing.reason')}
          value={reason}
          onChange={(e) => setReason(e.target.value)}
        />
        <button type="submit">{t('billing.grantBtn')}</button>
      </form>

      <h2>{t('billing.ledger')}</h2>
      {ledger.length === 0 ? (
        <p className="hint">{t('billing.none')}</p>
      ) : (
        <ul className="list">
          {ledger.map((x) => (
            <li key={x.id}>
              {x.kind} {x.amount}{' '}
              <span className="muted">
                · {x.reason ?? ''} · = {x.balance_after}
              </span>
            </li>
          ))}
        </ul>
      )}

      <h2>{t('billing.rateCards')}</h2>
      <ul className="list">
        {rates.map((r) => (
          <li key={r.id}>
            {r.model_id}{' '}
            <span className="muted">
              · {r.provider} · {r.unit} · in {r.credits_per_input} / out{' '}
              {r.credits_per_output}
            </span>
          </li>
        ))}
      </ul>

      <h2>{t('billing.usage')}</h2>
      {usage.length === 0 ? (
        <p className="hint">{t('billing.none')}</p>
      ) : (
        <ul className="list">
          {usage.map((u) => (
            <li key={u.id}>
              {u.op}{' '}
              <span className="muted">
                · {u.model_id ?? '-'} · {u.basis} · in {u.units_in}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
