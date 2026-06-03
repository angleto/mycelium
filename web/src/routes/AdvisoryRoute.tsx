import { useEffect, useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import { useFocus } from '../lib/focus'
import { TagPickerGrid } from '../components/TagPickerGrid'
import type { components } from '../api/schema'

type Feasible = components['schemas']['FeasibleTaskOut']
type Errand = components['schemas']['ErrandItemOut']
type Tag = components['schemas']['TagOut']
type WhatNowIn = components['schemas']['WhatNowIn']

const NECESSITIES = ['must', 'should', 'could'] as const
type Necessity = (typeof NECESSITIES)[number]

// Local wall-clock formatted for <input type="datetime-local"> (no tz).
function toLocalInput(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
}

// Deterministic advisory layer (FR-13/14): explainable ranking, same
// input -> same result. The UI is a thin form over /advisory/*; the core
// decides (deadline urgency + selection), the page only scopes + shows.
export function AdvisoryRoute() {
  const { t } = useTranslation()
  const { focusIds, active: focusActive } = useFocus()
  // (#1) default window_start to now; the picker is an Advanced override.
  const [start, setStart] = useState(() => toLocalInput(new Date()))
  const [advanced, setAdvanced] = useState(false)
  const [dur, setDur] = useState(60)
  const [loc, setLoc] = useState('')
  const [ctxTags, setCtxTags] = useState('')
  const [anyTags, setAnyTags] = useState<string[]>([])
  const [maxPriority, setMaxPriority] = useState('')
  const [minNec, setMinNec] = useState<Necessity | ''>('')
  const [tags, setTags] = useState<Tag[]>([])
  const [feasible, setFeasible] = useState<Feasible[] | null>(null)
  const [ctx, setCtx] = useState('')
  const [eLoc, setELoc] = useState('')
  const [errands, setErrands] = useState<Errand[] | null>(null)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    void (async () => {
      const { data } = await api.GET('/tags', { params: { header: workspaceHeader() } })
      if (data) setTags(data.filter((g) => g.kind === 'generic' && g.status === 'active'))
    })()
  }, [])

  function toggleTag(id: string) {
    setAnyTags((cur) => (cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id]))
  }

  async function onWhatNow(e: FormEvent) {
    e.preventDefault()
    setErr(null)
    const body: WhatNowIn = {
      // tz-AWARE: a datetime-local is naive; toISOString() yields an
      // offset-aware UTC value so the core never mixes naive+aware.
      window_start: start ? new Date(start).toISOString() : null,
      duration_minutes: dur,
      location: loc || null,
      context_tags: ctxTags
        .split(',')
        .map((s) => s.trim())
        .filter(Boolean),
    }
    // (#3) selection: send each selector only when active. Focus that is
    // active-but-empty (a client with no pushed projects) must NOT zero
    // out results, so guard on focusIds.length (not just `active`).
    if (focusActive && focusIds.length > 0) body.focus_tag_ids = focusIds
    if (anyTags.length > 0) body.any_tag_ids = anyTags
    if (maxPriority) body.max_priority = Number(maxPriority)
    if (minNec) body.min_necessity = minNec
    const { data, error } = await api.POST('/advisory/what-now', {
      params: { header: workspaceHeader() },
      body,
    })
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    setFeasible(data.ranked)
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
          {t('advisory.duration')}
          <input
            type="number"
            value={dur}
            onChange={(e) => setDur(Number(e.target.value))}
          />
          <span className="hint">{t('advisory.durationHint')}</span>
        </label>
        <label>
          {t('advisory.location')}
          <input value={loc} onChange={(e) => setLoc(e.target.value)} />
        </label>
        <label>
          {t('advisory.ctxHeading')}
          <input value={ctxTags} onChange={(e) => setCtxTags(e.target.value)} />
        </label>
        {focusActive && focusIds.length > 0 && (
          <span className="chip">{t('advisory.scopeFocus')}</span>
        )}
        <button type="submit">{t('advisory.ask')}</button>
      </form>

      <div className="row">
        <label>
          {t('advisory.maxPriority')}
          <input
            type="number"
            min={1}
            max={25}
            value={maxPriority}
            onChange={(e) => setMaxPriority(e.target.value)}
          />
          <span className="hint">{t('advisory.maxPriorityHint')}</span>
        </label>
        <div
          className="viewtabs"
          role="radiogroup"
          aria-label={t('advisory.necessityFloor')}
        >
          {(['', ...NECESSITIES] as const).map((n) => (
            <button
              type="button"
              key={n || 'any'}
              role="radio"
              aria-checked={minNec === n}
              className={'viewtabs__tab' + (minNec === n ? ' viewtabs__tab--active' : '')}
              onClick={() => setMinNec(n)}
            >
              {n
                ? t(`advisory.nec${n.charAt(0).toUpperCase()}${n.slice(1)}` as const)
                : t('advisory.necessityAny')}
            </button>
          ))}
        </div>
      </div>

      {tags.length > 0 && (
        <div className="row">
          <span className="muted">{t('advisory.tagsHeading')}</span>
          <TagPickerGrid
            tags={tags}
            selected={anyTags}
            onToggle={toggleTag}
            searchable={false}
          />
        </div>
      )}

      <p className="hint">{t('advisory.orHelp')}</p>

      <div className="row">
        <button
          type="button"
          className="btn--ghost btn--sm"
          aria-expanded={advanced}
          onClick={() => setAdvanced((v) => !v)}
        >
          {t('advisory.advancedStart')}
        </button>
        {advanced && (
          <label>
            {t('advisory.windowStart')}
            <input
              type="datetime-local"
              value={start}
              onChange={(e) => setStart(e.target.value)}
            />
          </label>
        )}
      </div>

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
