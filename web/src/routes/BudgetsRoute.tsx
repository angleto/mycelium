import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import { useSession } from '../auth/useSession'
import type { components } from '../shared'

type Budget = components['schemas']['BudgetOut']
type Consumption = components['schemas']['ConsumptionOut']
type Plan = components['schemas']['BudgetPlanOut']
type Period = components['schemas']['BudgetPeriod']

const PERIODS: Period[] = ['month', 'quarter', 'year', 'custom']

export function BudgetsRoute() {
  const { t } = useTranslation()
  const session = useSession()
  const activeId = session?.workspaceId
  const [list, setList] = useState<Budget[]>([])
  const [name, setName] = useState('')
  const [period, setPeriod] = useState<Period>('month')
  const [pStart, setPStart] = useState('')
  const [pEnd, setPEnd] = useState('')
  const [amount, setAmount] = useState(0)
  const [sel, setSel] = useState('')
  const [cons, setCons] = useState<Consumption | null>(null)
  const [plan, setPlan] = useState<Plan | null>(null)
  const [err, setErr] = useState<string | null>(null)

  const loadList = useCallback(async () => {
    const { data, error } = await api.GET('/budgets', {
      params: { header: workspaceHeader() },
    })
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    setList(data)
  }, [])

  useEffect(() => {
    let active = true
    void (async () => {
      const { data } = await api.GET('/budgets', {
        params: { header: workspaceHeader() },
      })
      if (active && data) setList(data)
    })()
    return () => {
      active = false
    }
  }, [activeId])

  useEffect(() => {
    if (!sel) return
    let active = true
    void (async () => {
      const h = workspaceHeader()
      const [c, p] = await Promise.all([
        api.GET('/budgets/{budget_id}/consumption', {
          params: { header: h, path: { budget_id: sel } },
        }),
        api.GET('/advisory/budget/{budget_id}/plan', {
          params: { header: h, path: { budget_id: sel } },
        }),
      ])
      if (!active) return
      setCons(c.data ?? null)
      setPlan(p.data ?? null)
    })()
    return () => {
      active = false
    }
  }, [sel])

  async function onCreate(e: FormEvent) {
    e.preventDefault()
    setErr(null)
    const { error } = await api.POST('/budgets', {
      params: { header: workspaceHeader() },
      body: {
        name,
        period_kind: period,
        period_start: pStart,
        period_end: pEnd,
        amount,
        currency: 'EUR',
      },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    setName('')
    await loadList()
  }

  return (
    <section className="card">
      <h1>{t('budgets.title')}</h1>
      {err && <p className="err">{err}</p>}

      <form onSubmit={(e) => void onCreate(e)} className="row">
        <input
          required
          placeholder={t('budgets.name')}
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        <select value={period} onChange={(e) => setPeriod(e.target.value as Period)}>
          {PERIODS.map((p) => (
            <option key={p} value={p}>
              {p}
            </option>
          ))}
        </select>
        <input
          type="date"
          required
          value={pStart}
          onChange={(e) => setPStart(e.target.value)}
        />
        <input
          type="date"
          required
          value={pEnd}
          onChange={(e) => setPEnd(e.target.value)}
        />
        <input
          type="number"
          required
          value={amount}
          onChange={(e) => setAmount(Number(e.target.value))}
        />
        <button type="submit">{t('budgets.create')}</button>
      </form>

      {list.length === 0 ? (
        <p className="hint">{t('budgets.none')}</p>
      ) : (
        <label>
          {t('budgets.title')}{' '}
          <select value={sel} onChange={(e) => setSel(e.target.value)}>
            <option value="">--</option>
            {list.map((b) => (
              <option key={b.id} value={b.id}>
                {b.name} ({b.amount} {b.currency})
              </option>
            ))}
          </select>
        </label>
      )}

      {sel && cons && (
        <dl className="kv">
          <dt>{t('budgets.consumed')}</dt>
          <dd>
            {cons.consumed} {cons.currency}
          </dd>
          <dt>{t('budgets.residual')}</dt>
          <dd>
            {cons.residual} {cons.currency}
          </dd>
        </dl>
      )}

      {sel && plan && (
        <>
          <h2>{t('budgets.plan')}</h2>
          <p className="muted">
            {t('budgets.allocated')}: {plan.allocated} / {plan.amount} {plan.currency}
          </p>
          <p>{t('budgets.selected')}</p>
          <ul className="list">
            {plan.selected.map((s) => (
              <li key={s.task_id}>
                {s.title}{' '}
                <span className="muted">
                  · {s.cost} · {s.necessity} · v{s.value}
                </span>
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  )
}
