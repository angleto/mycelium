import { useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import type { components } from '../api/schema'

type Feasible = components['schemas']['FeasibleTaskOut']
type Errand = components['schemas']['ErrandItemOut']

// Deterministic advisory layer (FR-13/14): explainable ranking, same
// input -> same result. The UI is a thin form over /advisory/*.
export function AdvisoryRoute() {
  const { t } = useTranslation()
  const [start, setStart] = useState('')
  const [dur, setDur] = useState(60)
  const [loc, setLoc] = useState('')
  const [feasible, setFeasible] = useState<Feasible[] | null>(null)
  const [ctx, setCtx] = useState('')
  const [eLoc, setELoc] = useState('')
  const [errands, setErrands] = useState<Errand[] | null>(null)
  const [err, setErr] = useState<string | null>(null)

  async function onWhatNow(e: FormEvent) {
    e.preventDefault()
    setErr(null)
    const { data, error } = await api.POST('/advisory/what-now', {
      params: { header: workspaceHeader() },
      body: {
        window_start: `${start}:00`,
        duration_minutes: dur,
        location: loc || null,
      },
    })
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    setFeasible(data)
  }

  async function onErrands(e: FormEvent) {
    e.preventDefault()
    setErr(null)
    const { data, error } = await api.POST('/advisory/errands', {
      params: { header: workspaceHeader() },
      body: { location: eLoc || null, context: ctx || null },
    })
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    setErrands(data)
  }

  return (
    <section className="card">
      <h1>{t('advisory.title')}</h1>
      {err && <p className="err">{err}</p>}

      <form onSubmit={(e) => void onWhatNow(e)} className="row">
        <label>
          {t('advisory.windowStart')}
          <input
            type="datetime-local"
            required
            value={start}
            onChange={(e) => setStart(e.target.value)}
          />
        </label>
        <label>
          {t('advisory.duration')}
          <input
            type="number"
            min={5}
            value={dur}
            onChange={(e) => setDur(Number(e.target.value))}
          />
        </label>
        <label>
          {t('advisory.location')}
          <input value={loc} onChange={(e) => setLoc(e.target.value)} />
        </label>
        <button type="submit">{t('advisory.ask')}</button>
      </form>

      {feasible && (
        <>
          <h2>{t('advisory.feasible')}</h2>
          {feasible.length === 0 ? (
            <p className="hint">{t('advisory.none')}</p>
          ) : (
            <ul className="list">
              {feasible.map((f) => (
                <li key={f.task_id}>
                  {f.title}{' '}
                  <span className="muted">
                    · {f.necessity} · P{f.priority} · {f.remaining_minutes}{' '}
                    {t('advisory.remaining')}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </>
      )}

      <h2>{t('advisory.errandsTitle')}</h2>
      <form onSubmit={(e) => void onErrands(e)} className="row">
        <label>
          {t('advisory.location')}
          <input value={eLoc} onChange={(e) => setELoc(e.target.value)} />
        </label>
        <label>
          {t('advisory.context')}
          <input value={ctx} onChange={(e) => setCtx(e.target.value)} />
        </label>
        <button type="submit">{t('advisory.bundle')}</button>
      </form>
      {errands && (
        <ul className="list">
          {errands.length === 0 ? (
            <li className="hint">{t('advisory.errNone')}</li>
          ) : (
            errands.map((x) => (
              <li key={x.task_id}>
                {x.title}{' '}
                <span className="muted">
                  · {x.location ?? '-'} · {x.necessity} · P{x.priority}
                </span>
              </li>
            ))
          )}
        </ul>
      )}
    </section>
  )
}
