import { useEffect, useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import { useSession } from '../auth/useSession'
import type { components } from '../shared'

type Executor = components['schemas']['ExecutorOut']

const EMPTY = {
  kind: 'llm_agent' as 'llm_agent' | 'human',
  name: '',
  provider: '',
  model_id: '',
  context_switch_cost_minutes: 0,
  max_parallel: 4,
  credit_budget: '',
  credit_rate_per_hour: '0',
  capabilities: '',
  enabled: true,
}
type Form = typeof EMPTY

const csv = (s: string): string[] =>
  s
    .split(',')
    .map((x) => x.trim())
    .filter(Boolean)

// ADR-0025 P2 — executor registry. Humans are auto-seeded (one per
// member); their context-switch cost / capabilities are editable.
// LLM agents are configured here: provider/model, concurrency cap,
// credit budget+rate, capabilities. Mutations are owner-gated by the
// server (the form just surfaces a denial).
export function ExecutorsAdmin() {
  const { t } = useTranslation()
  const session = useSession()
  const [rows, setRows] = useState<Executor[]>([])
  const [form, setForm] = useState<Form>(EMPTY)
  const [edit, setEdit] = useState<string | 'new' | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [tick, setTick] = useState(0)

  useEffect(() => {
    let active = true
    void (async () => {
      const { data, error } = await api.GET('/executors', {
        params: { header: workspaceHeader() },
      })
      if (!active) return
      if (error) {
        setErr(errMessage(error))
        return
      }
      setRows(data ?? [])
    })()
    return () => {
      active = false
    }
  }, [session?.workspaceId, tick])

  function start(e: Executor | null) {
    setErr(null)
    if (e) {
      setEdit(e.id)
      setForm({
        kind: e.kind,
        name: e.name,
        provider: e.provider ?? '',
        model_id: e.model_id ?? '',
        context_switch_cost_minutes: e.context_switch_cost_minutes,
        max_parallel: e.max_parallel,
        credit_budget: e.credit_budget == null ? '' : String(e.credit_budget),
        credit_rate_per_hour: String(e.credit_rate_per_hour),
        capabilities: (e.capability_tags ?? []).join(', '),
        enabled: e.enabled,
      })
    } else {
      setEdit('new')
      setForm(EMPTY)
    }
  }

  async function save(ev: FormEvent) {
    ev.preventDefault()
    setErr(null)
    const h = workspaceHeader()
    const common = {
      name: form.name,
      provider: form.provider || null,
      model_id: form.model_id || null,
      context_switch_cost_minutes: Number(form.context_switch_cost_minutes),
      max_parallel: Number(form.max_parallel),
      credit_budget: form.credit_budget === '' ? null : form.credit_budget,
      credit_rate_per_hour: form.credit_rate_per_hour || '0',
      capability_tags: csv(form.capabilities),
      enabled: form.enabled,
    }
    if (edit === 'new') {
      const { error } = await api.POST('/executors', {
        params: { header: h },
        body: { kind: form.kind, ...common },
      })
      if (error) {
        setErr(errMessage(error))
        return
      }
    } else {
      const cur = rows.find((r) => r.id === edit)
      if (!cur) return
      const { error } = await api.PATCH('/executors/{executor_id}', {
        params: { header: h, path: { executor_id: edit as string } },
        body: { expected_version: cur.version, ...common },
      })
      if (error) {
        setErr(errMessage(error))
        return
      }
    }
    setEdit(null)
    setTick((n) => n + 1)
  }

  async function remove(id: string) {
    if (!window.confirm(t('exec.confirmDelete'))) return
    setErr(null)
    const { error } = await api.DELETE('/executors/{executor_id}', {
      params: { header: workspaceHeader(), path: { executor_id: id } },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    setTick((n) => n + 1)
  }

  return (
    <section className="card">
      <h2>{t('exec.title')}</h2>
      <p className="hint">{t('exec.intro')}</p>
      {err && <p className="err">{err}</p>}
      <ul className="list">
        {rows.map((e) => (
          <li key={e.id} className="mch__row">
            <span className="grow">
              <strong>{e.name}</strong>{' '}
              <span className="tag tag--muted">{e.kind}</span>
              {e.kind === 'llm_agent' ? (
                <span className="muted">
                  {' '}
                  · {e.provider || '—'}/{e.model_id || '—'} · ∥
                  {e.max_parallel} · {e.credit_rate_per_hour}cr/h
                  {e.credit_budget != null
                    ? ` · ${t('exec.budget')} ${e.credit_budget}`
                    : ''}
                </span>
              ) : (
                <span className="muted">
                  {' '}
                  · {t('exec.switch')} {e.context_switch_cost_minutes}m
                </span>
              )}
              {(e.capability_tags ?? []).length > 0 && (
                <span className="muted">
                  {' '}
                  · {(e.capability_tags ?? []).join(', ')}
                </span>
              )}
              {!e.enabled && (
                <span className="tag tag--muted"> {t('exec.disabled')}</span>
              )}
            </span>
            <button
              type="button"
              className="btn--sm btn--ghost"
              onClick={() => start(e)}
            >
              {t('exec.edit')}
            </button>
            <button
              type="button"
              className="btn--sm btn--danger"
              onClick={() => void remove(e.id)}
            >
              {t('exec.delete')}
            </button>
          </li>
        ))}
      </ul>
      {edit === null ? (
        <button type="button" className="btn--sm" onClick={() => start(null)}>
          {t('exec.newAgent')}
        </button>
      ) : (
        <form onSubmit={(e) => void save(e)} className="card card--running">
          <h3>{edit === 'new' ? t('exec.newAgent') : t('exec.editTitle')}</h3>
          <div className="row">
            {edit !== 'new' && (
              <label>
                {t('exec.kind')}
                <span className="tag tag--muted">{form.kind}</span>
              </label>
            )}
            <label>
              {t('exec.name')}
              <input
                required
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
              />
            </label>
            <label>
              {t('exec.capabilities')}
              <input
                placeholder="python, writing"
                value={form.capabilities}
                onChange={(e) =>
                  setForm({ ...form, capabilities: e.target.value })
                }
              />
            </label>
          </div>
          {form.kind === 'llm_agent' ? (
            <div className="row">
              <label>
                {t('exec.provider')}
                <input
                  value={form.provider}
                  onChange={(e) =>
                    setForm({ ...form, provider: e.target.value })
                  }
                />
              </label>
              <label>
                {t('exec.model')}
                <input
                  value={form.model_id}
                  onChange={(e) =>
                    setForm({ ...form, model_id: e.target.value })
                  }
                />
              </label>
              <label>
                {t('exec.maxParallel')}
                <input
                  type="number"
                  style={{ width: '4.5rem' }}
                  value={form.max_parallel}
                  onChange={(e) =>
                    setForm({ ...form, max_parallel: Number(e.target.value) })
                  }
                />
              </label>
              <label>
                {t('exec.rate')}
                <input
                  style={{ width: '6rem' }}
                  value={form.credit_rate_per_hour}
                  onChange={(e) =>
                    setForm({ ...form, credit_rate_per_hour: e.target.value })
                  }
                />
              </label>
              <label>
                {t('exec.budget')}
                <input
                  style={{ width: '6rem' }}
                  placeholder="∞"
                  value={form.credit_budget}
                  onChange={(e) =>
                    setForm({ ...form, credit_budget: e.target.value })
                  }
                />
              </label>
            </div>
          ) : (
            <div className="row">
              <label>
                {t('exec.switch')}
                <input
                  type="number"
                  style={{ width: '5rem' }}
                  value={form.context_switch_cost_minutes}
                  onChange={(e) =>
                    setForm({
                      ...form,
                      context_switch_cost_minutes: Number(e.target.value),
                    })
                  }
                />
              </label>
            </div>
          )}
          <label className="row">
            <input
              type="checkbox"
              checked={form.enabled}
              onChange={(e) =>
                setForm({ ...form, enabled: e.target.checked })
              }
            />
            {t('exec.enabled')}
          </label>
          <div className="row">
            <button type="submit">{t('exec.saveBtn')}</button>
            <button
              type="button"
              className="btn--ghost"
              onClick={() => setEdit(null)}
            >
              {t('exec.cancel')}
            </button>
          </div>
        </form>
      )}
    </section>
  )
}
