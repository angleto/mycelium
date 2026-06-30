import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import { useSession } from '../auth/useSession'
import type { components } from '../api/schema'

const LED_PAGE = 50

type Ledger = components['schemas']['LedgerOut']
type Rate = components['schemas']['RateCardOut']
type Usage = components['schemas']['UsageOut']

export function BillingRoute() {
  const { t } = useTranslation()
  const session = useSession()
  const activeId = session?.workspaceId
  const [balance, setBalance] = useState<string>('')
  const [ledger, setLedger] = useState<Ledger[]>([])
  const [ledOffset, setLedOffset] = useState(0)
  const [ledMore, setLedMore] = useState(false)
  const [ledLoading, setLedLoading] = useState(false)
  const [rates, setRates] = useState<Rate[]>([])
  const [usage, setUsage] = useState<Usage[]>([])
  const [amount, setAmount] = useState(0)
  const [reason, setReason] = useState('')
  const [role, setRole] = useState<string>('')
  const [err, setErr] = useState<string | null>(null)
  // Rate-card upsert form (admin): price a model for our_key billing.
  const [rcModel, setRcModel] = useState('')
  const [rcProvider, setRcProvider] = useState('scaleway')
  const [rcCostIn, setRcCostIn] = useState('')
  const [rcCostOut, setRcCostOut] = useState('')
  const [rcMarkup, setRcMarkup] = useState('1')
  const isAdmin = role === 'owner' || role === 'admin'

  const resetLedger = useCallback(async () => {
    const { data } = await api.GET('/billing/ledger', {
      params: {
        header: workspaceHeader(),
        query: { limit: LED_PAGE, offset: 0 },
      },
    })
    if (!data) return
    setLedger(data)
    setLedOffset(data.length)
    setLedMore(data.length === LED_PAGE)
  }, [])

  const moreLedger = useCallback(async () => {
    if (ledLoading || !ledMore) return
    setLedLoading(true)
    const { data } = await api.GET('/billing/ledger', {
      params: {
        header: workspaceHeader(),
        query: { limit: LED_PAGE, offset: ledOffset },
      },
    })
    setLedLoading(false)
    if (!data) return
    setLedger((p) => [...p, ...data])
    setLedOffset((o) => o + data.length)
    setLedMore(data.length === LED_PAGE)
  }, [ledLoading, ledMore, ledOffset])

  const reload = useCallback(async () => {
    const h = workspaceHeader()
    const [b, r, u] = await Promise.all([
      api.GET('/billing/balance', { params: { header: h } }),
      api.GET('/billing/rate-cards', { params: { header: h } }),
      api.GET('/billing/usage', { params: { header: h } }),
    ])
    if (b.data) setBalance(b.data.balance)
    if (r.data) setRates(r.data)
    if (u.data) setUsage(u.data)
    await resetLedger()
    const ws = await api.GET('/workspaces')
    const me = ws.data?.find((w) => w.id === activeId)
    if (me) setRole(me.role)
  }, [activeId, resetLedger])

  useEffect(() => {
    let active = true
    void (async () => {
      const h = workspaceHeader()
      const [b, r, u] = await Promise.all([
        api.GET('/billing/balance', { params: { header: h } }),
        api.GET('/billing/rate-cards', { params: { header: h } }),
        api.GET('/billing/usage', { params: { header: h } }),
      ])
      if (!active) return
      if (b.data) setBalance(b.data.balance)
      if (r.data) setRates(r.data)
      if (u.data) setUsage(u.data)
      await resetLedger()
      const ws = await api.GET('/workspaces')
      if (active) {
        const me = ws.data?.find((w) => w.id === activeId)
        if (me) setRole(me.role)
      }
    })()
    return () => {
      active = false
    }
  }, [activeId, resetLedger])

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

  async function onUpsertRate(e: FormEvent) {
    e.preventDefault()
    setErr(null)
    if (!rcModel.trim() || !rcProvider.trim()) return
    const { error } = await api.POST('/billing/rate-cards', {
      params: { header: workspaceHeader() },
      body: {
        model_id: rcModel.trim(),
        provider: rcProvider.trim(),
        provider_cost_per_input: rcCostIn.trim() || null,
        provider_cost_per_output: rcCostOut.trim() || null,
        markup: rcMarkup.trim() || '1',
      },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    setRcModel('')
    setRcCostIn('')
    setRcCostOut('')
    setRcMarkup('1')
    await reload()
  }

  return (
    <section className="card">
      <h1>{t('billing.title')}</h1>
      <p className="hint">{t('billing.intro')}</p>
      {err && <p className="err">{err}</p>}
      <p>
        <strong>{t('billing.balance')}:</strong> {balance || '0'}
      </p>

      {!isAdmin && <p className="hint">{t('billing.grantAdminOnly')}</p>}
      {isAdmin && (
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
      )}

      <h2>{t('billing.ledger')}</h2>
      <p className="hint">{t('billing.ledgerHint')}</p>
      {ledger.length === 0 ? (
        <p className="hint">{t('billing.none')}</p>
      ) : (
        <div
          className="scrollbox"
          onScroll={(e) => {
            const el = e.currentTarget
            if (
              el.scrollHeight - el.scrollTop - el.clientHeight < 80 &&
              ledMore &&
              !ledLoading
            ) {
              void moreLedger()
            }
          }}
        >
          <ul className="list">
            {ledger.map((x) => (
              <li key={x.id}>
                <strong>
                  {x.kind === 'grant'
                    ? t('billing.kindGrant')
                    : t('billing.kindDebit')}
                </strong>{' '}
                {x.kind === 'grant' ? '+' : '−'}
                {x.amount}{' '}
                <span className="muted">
                  · {x.reason ?? ''} · {t('billing.balanceAfter')}{' '}
                  {x.balance_after}
                </span>
              </li>
            ))}
          </ul>
          {ledLoading && <p className="hint">{t('billing.loading')}</p>}
          {!ledMore && !ledLoading && (
            <p className="hint">{t('billing.end')}</p>
          )}
        </div>
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
      {isAdmin && (
        <form onSubmit={(e) => void onUpsertRate(e)} className="rate-upsert">
          <h3>{t('billing.rateAdd')}</h3>
          <p className="hint">{t('billing.rateHint')}</p>
          <div className="row">
            <input
              required
              placeholder={t('billing.rateModel')}
              value={rcModel}
              onChange={(e) => setRcModel(e.target.value)}
            />
            <input
              required
              placeholder={t('billing.rateProvider')}
              value={rcProvider}
              onChange={(e) => setRcProvider(e.target.value)}
            />
          </div>
          <div className="row">
            <input
              type="number"
              step="any"
              min="0"
              placeholder={t('billing.rateCostIn')}
              value={rcCostIn}
              onChange={(e) => setRcCostIn(e.target.value)}
            />
            <input
              type="number"
              step="any"
              min="0"
              placeholder={t('billing.rateCostOut')}
              value={rcCostOut}
              onChange={(e) => setRcCostOut(e.target.value)}
            />
            <input
              type="number"
              step="any"
              min="0"
              placeholder={t('billing.rateMarkup')}
              value={rcMarkup}
              onChange={(e) => setRcMarkup(e.target.value)}
            />
            <button type="submit">{t('billing.rateSave')}</button>
          </div>
        </form>
      )}

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
