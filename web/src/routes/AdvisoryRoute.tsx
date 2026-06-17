import { useEffect, useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
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

const BUCKET_LABEL: Record<string, string> = {
  overdue: 'bucketOverdue',
  at_risk: 'bucketAtRisk',
  tight: 'bucketTight',
  comfortable: 'bucketComfortable',
  none: 'bucketNone',
}

// Local wall-clock formatted for <input type="datetime-local"> (no tz).
function toLocalInput(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function sameDay(a: Date, b: Date): boolean {
  return (
    a.getFullYear() === b.getFullYear() &&
    a.getMonth() === b.getMonth() &&
    a.getDate() === b.getDate()
  )
}

function bucketClass(b: string): string {
  return b === 'overdue'
    ? 'urg--overdue'
    : b === 'at_risk'
      ? 'urg--at_risk'
      : b === 'tight'
        ? 'urg--tight'
        : b === 'comfortable'
          ? 'urg--comfortable'
          : ''
}

// Deterministic advisory layer (FR-13/14): explainable ranking, same
// input -> same result. The core decides (deadline urgency + selection);
// the page scopes, shows WHY (badges), and only narrates on demand.
export function AdvisoryRoute() {
  const { t } = useTranslation()
  const { scopeTagIds, active: focusActive, clientName, projectName } = useFocus()
  // (#1) default window_start to now; the picker is an Advanced override.
  const [start, setStart] = useState(() => toLocalInput(new Date()))
  const [advanced, setAdvanced] = useState(false)
  const [dur, setDur] = useState(60)
  const [loc, setLoc] = useState('')
  const [ctxTags, setCtxTags] = useState('')
  const [anyTags, setAnyTags] = useState<string[]>([])
  const [minPriority, setMinPriority] = useState('')
  const [minNec, setMinNec] = useState<Necessity | ''>('')
  const [tags, setTags] = useState<Tag[]>([])
  const [feasible, setFeasible] = useState<Feasible[] | null>(null)
  // Tasks that clear every filter except they need more time than the
  // window. Surfaced apart (collapsed) so a too-long overdue/at-risk task
  // is not silently hidden when the slot is shorter than its effort.
  const [overWindow, setOverWindow] = useState<Feasible[]>([])
  const [showOverWindow, setShowOverWindow] = useState(false)
  // The window_start actually POSTed, so relative-due labels use the SAME
  // clock the core ranked against (no server-substituted now() drift).
  const [postedWindow, setPostedWindow] = useState<Date | null>(null)
  // Opt-in AI narration state (req #4b). Degrades gracefully: until T3
  // wires the metered narrator, the API returns narrated=false.
  const [asked, setAsked] = useState(false)
  const [narrating, setNarrating] = useState(false)
  const [narration, setNarration] = useState<string | null>(null)
  const [narrationModel, setNarrationModel] = useState<string | null>(null)
  const [narrated, setNarrated] = useState(false)
  const [ctx, setCtx] = useState('')
  const [eLoc, setELoc] = useState('')
  const [errands, setErrands] = useState<Errand[] | null>(null)
  // (T10) errands is a secondary planning tool, collapsed by default so it
  // does not compete with the primary 'what can I do now' answer.
  const [showOther, setShowOther] = useState(false)
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

  // Build the request from the current form. narrate is opt-in; the same
  // selection params drive both the deterministic plan and its narration.
  function buildBody(narrate: boolean): { body: WhatNowIn; ws: Date } {
    const ws = start ? new Date(start) : new Date()
    const body: WhatNowIn = {
      // tz-AWARE: datetime-local is naive; toISOString() is offset-aware
      // so the core never mixes naive+aware in the slack subtraction.
      window_start: ws.toISOString(),
      duration_minutes: dur,
      location: loc || null,
      context_tags: ctxTags
        .split(',')
        .map((s) => s.trim())
        .filter(Boolean),
      narrate,
    }
    // Focus is a hard SCOPE on the server. Send the scope tag ids (client
    // tag + its projects, or the single project tag): a client focus then
    // scopes even before the project list loads, instead of silently
    // showing everything. Empty == inactive, so nothing zeroes out.
    if (focusActive && scopeTagIds.length > 0) body.focus_tag_ids = scopeTagIds
    if (anyTags.length > 0) body.any_tag_ids = anyTags
    if (minPriority) body.min_priority = Number(minPriority)
    if (minNec) body.min_necessity = minNec
    return { body, ws }
  }

  async function onWhatNow(e: FormEvent) {
    e.preventDefault()
    setErr(null)
    const { body, ws } = buildBody(false)
    const { data, error } = await api.POST('/advisory/what-now', {
      params: { header: workspaceHeader() },
      body,
    })
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    setPostedWindow(ws)
    setFeasible(data.ranked)
    setOverWindow(data.over_window ?? [])
    // Fresh ranking resets any prior narration.
    setAsked(false)
    setNarrating(false)
    setNarration(null)
    setNarrationModel(null)
    setNarrated(false)
  }

  async function onHelpDecide() {
    setErr(null)
    setAsked(true)
    setNarrating(true)
    const { body, ws } = buildBody(true)
    const { data, error } = await api.POST('/advisory/what-now', {
      params: { header: workspaceHeader() },
      body,
    })
    setNarrating(false)
    if (error || !data) {
      // Keep the ranked list intact; surface the error only.
      setErr(errMessage(error))
      return
    }
    setPostedWindow(ws)
    setFeasible(data.ranked)
    setOverWindow(data.over_window ?? [])
    setNarrated(!!data.narrated)
    setNarration(data.narration ?? null)
    setNarrationModel(data.narration_model ?? null)
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

  // Relative due label against the POSTed window (client/server clock
  // agreement): 'overdue' / 'due today' / 'due in Nd|Nh' / 'no deadline'.
  function relativeDue(due: string | null): string {
    const base = postedWindow ?? new Date()
    if (!due) return t('advisory.bucketNone')
    const d = new Date(due)
    const ms = d.getTime() - base.getTime()
    if (ms < 0) return t('advisory.dueOverdue')
    if (sameDay(d, base)) return t('advisory.dueToday')
    const days = Math.round(ms / 86_400_000)
    if (days >= 1) return t('advisory.dueIn', { n: days, unit: 'd' })
    const hours = Math.max(1, Math.round(ms / 3_600_000))
    return t('advisory.dueIn', { n: hours, unit: 'h' })
  }

  // One ranked task row, shared by the feasible list and the over-window
  // list (same facts/badges; the partition is the only difference).
  function renderItem(f: Feasible) {
    return (
      <li key={f.task_id}>
        <Link to={`/tasks/${f.task_id}`} className="advisory__task">
          {f.title}
        </Link>{' '}
        <span className="muted">
          · {f.necessity} · P{f.priority} · {f.remaining_minutes} {t('advisory.remaining')} ·{' '}
          {relativeDue(f.due_date)}
          {f.slack_minutes != null && (
            <>
              {' '}
              · {t('advisory.slack')} {f.slack_minutes}m
            </>
          )}
        </span>
        {f.deadline_bucket !== 'none' && (
          <span className={`urg ${bucketClass(f.deadline_bucket)}`}>
            {t(`advisory.${BUCKET_LABEL[f.deadline_bucket]}` as const)}
          </span>
        )}
      </li>
    )
  }

  return (
    <section className="card">
      <h1>{t('advisory.title')}</h1>
      {err && <p className="err">{err}</p>}

      <form onSubmit={(e) => void onWhatNow(e)}>
        <div className="advform">
          <label>
            <span className="cap">{t('advisory.duration')}</span>
            <input
              type="number"
              min={1}
              value={dur}
              onChange={(e) => setDur(Number(e.target.value))}
            />
            <span className="hint">{t('advisory.durationHint')}</span>
          </label>
          <label>
            <span className="cap">{t('advisory.location')}</span>
            <input value={loc} onChange={(e) => setLoc(e.target.value)} />
            <span className="hint">{t('advisory.locationHint')}</span>
          </label>
          <label>
            <span className="cap">{t('advisory.minPriority')}</span>
            <input
              type="number"
              min={1}
              max={25}
              value={minPriority}
              onChange={(e) => setMinPriority(e.target.value)}
            />
            <span className="hint">{t('advisory.minPriorityHint')}</span>
          </label>
        </div>

        <div className="advrow">
          <span className="cap">{t('advisory.necessityFloor')}</span>
          <div className="viewtabs" role="radiogroup" aria-label={t('advisory.necessityFloor')}>
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
          <div className="advrow advrow--tags">
            <span className="cap">{t('advisory.tagsHeading')}</span>
            <TagPickerGrid tags={tags} selected={anyTags} onToggle={toggleTag} searchable={false} />
          </div>
        )}

        <div className="advform__actions">
          {focusActive && (
            <span className="chip chip--scope" title={t('advisory.scopeFocusTip')}>
              <span className="chip__dot" />
              {projectName || clientName
                ? t('advisory.scopeFocusNamed', { name: projectName || clientName })
                : t('advisory.scopeFocus')}
            </span>
          )}
          <button type="submit">{t('advisory.ask')}</button>
        </div>
      </form>

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
      </div>
      {advanced && (
        <div className="advform">
          <label>
            <span className="cap">{t('advisory.windowStart')}</span>
            <input
              type="datetime-local"
              value={start}
              onChange={(e) => setStart(e.target.value)}
            />
            <span className="hint">{t('advisory.windowStartHint')}</span>
          </label>
          <label>
            <span className="cap">{t('advisory.ctxHeading')}</span>
            <input value={ctxTags} onChange={(e) => setCtxTags(e.target.value)} />
            <span className="hint">{t('advisory.ctxHint')}</span>
          </label>
        </div>
      )}

      {feasible && (
        <>
          <h2>{t('advisory.feasible')}</h2>
          {feasible.length === 0 ? (
            <p className="hint">{t('advisory.none')}</p>
          ) : (
            <ul className="list">{feasible.map(renderItem)}</ul>
          )}

          {overWindow.length > 0 && (
            <div className="advisory__overwindow">
              <button
                type="button"
                className="btn--ghost btn--sm"
                aria-expanded={showOverWindow}
                onClick={() => setShowOverWindow((v) => !v)}
              >
                {t('advisory.overWindowHeading', { count: overWindow.length })}
              </button>
              {showOverWindow && (
                <>
                  <p className="hint">{t('advisory.overWindowHint')}</p>
                  <ul className="list">{overWindow.map(renderItem)}</ul>
                </>
              )}
            </div>
          )}

          <div className="row">
            <button
              type="button"
              className="btn--ghost btn--sm"
              disabled={narrating}
              onClick={() => void onHelpDecide()}
            >
              {narrating ? t('advisory.aiThinking') : t('advisory.helpDecide')}
            </button>
          </div>
          {asked && !narrating && (
            <div className="aiadvice">
              {narrated && narration ? (
                <>
                  <h3>{t('advisory.aiHeading')}</h3>
                  <p>{narration}</p>
                  <p className="hint">
                    {t('advisory.aiDeterministic')}
                    {narrationModel ? ` · ${narrationModel}` : ''}
                  </p>
                </>
              ) : (
                <p className="muted">{t('advisory.aiUnavailable')}</p>
              )}
            </div>
          )}
        </>
      )}

      <hr className="sep" />
      <button
        type="button"
        className="btn--ghost btn--sm"
        aria-expanded={showOther}
        onClick={() => setShowOther((v) => !v)}
      >
        {t('advisory.otherTools')}
      </button>
      {showOther && (
        <>
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
                    <Link to={`/tasks/${x.task_id}`} className="advisory__task">
                      {x.title}
                    </Link>{' '}
                    <span className="muted">
                      · {x.location ?? '-'} · {x.necessity} · P{x.priority}
                    </span>
                  </li>
                ))
              )}
            </ul>
          )}
        </>
      )}
    </section>
  )
}
