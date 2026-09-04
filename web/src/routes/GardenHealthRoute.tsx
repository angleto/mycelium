import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api, workspaceHeader } from '../api/client'
import type { components } from '../shared'

// Garden health sensors (ADR-0035): one card per structural metric, with
// its reading, health floor, a 30-day sparkline, and a plain-language
// explanation. "Show, never judge": below the floor the value renders in
// muted red, never a verdict. No traffic lights, no single score.
//
// Each number is formatted for its kind (a probability as a %, a duration
// in human units, a graph score to a few digits) and carries its
// direction ("higher is better" / "the trend matters") plus 30-day
// context (the change and the range), so the reading is legible and
// interpretable instead of a raw float (task ec471b29). A metric whose
// absolute value is near-meaningless (Leiden modularity) leads with the
// 30-day change; sensors not yet wired to a data source are listed apart
// rather than shown as a permanently empty card.

type Health = components['schemas']['GardenHealthOut']
type Metric = components['schemas']['GardenHealthMetricOut']
type HealthEvent = components['schemas']['GardenHealthEventOut']
type Telemetry = components['schemas']['GardenLearningTelemetryOut']
type Drift = components['schemas']['GardenFeatureDeltaOut']

type Kind = 'pct' | 'duration' | 'bits' | 'scalar' | 'delta'
type Dir = 'higher' | 'lower' | 'trend' | 'signed'

interface Meta {
  kind: Kind
  dir: Dir
  // Absolute value is near-meaningless: lead with the 30-day change.
  trendLead?: boolean
  // Not yet wired to a data source: listed apart, never a faked empty.
  blocked?: boolean
  // A budget gauge (value vs a cap), not a health sensor: being BELOW the
  // floor is GOOD (under budget), so the alarm styling inverts (alarm when
  // at/over the cap) and the floor line reads as a "cap".
  gauge?: boolean
}

// Display order (ADR-0035), with per-metric presentation.
const META: Record<string, Meta> = {
  accept_rate_classify_7d: { kind: 'pct', dir: 'higher' },
  accept_rate_classify_30d: { kind: 'pct', dir: 'higher' },
  time_to_first_link: { kind: 'duration', dir: 'lower' },
  tag_entropy_local: { kind: 'bits', dir: 'higher' },
  leiden_modularity: { kind: 'scalar', dir: 'trend', trendLead: true },
  density_delta_7d: { kind: 'delta', dir: 'signed' },
  embedding_coverage: { kind: 'pct', dir: 'higher' },
  recall_at_k: { kind: 'pct', dir: 'higher' },
  // fungal_lag is now wired (WS-C6): a real median, or null+reason when
  // there are no distillations yet -- a live sensor, no longer "pending".
  fungal_lag: { kind: 'duration', dir: 'lower' },
  // Operational budget gauge (WS-F5): autonomous credits spent today vs the
  // daily cap. value/floor only when a cap is set; otherwise null+reason.
  autonomous_spend_today: { kind: 'scalar', dir: 'lower', gauge: true },
  // ADR-0048: retrieval_trace rows older than the retention window -- rows
  // the fuel_retention sweep should have pruned. Healthy ~0; growing means
  // the pruner is not running and the fuel table accumulates unbounded.
  trace_backlog: { kind: 'scalar', dir: 'lower' },
}
const ORDER = Object.keys(META)

function fmtDuration(s: number): string {
  const a = Math.abs(s)
  const sign = s < 0 ? '-' : ''
  if (a < 60) return `${Math.round(s)}s`
  if (a < 3600) return `${sign}${Math.round(a / 60)}m`
  if (a < 86400) {
    const h = Math.floor(a / 3600)
    const m = Math.round((a % 3600) / 60)
    return `${sign}${m ? `${h}h ${m}m` : `${h}h`}`
  }
  const d = Math.floor(a / 86400)
  const h = Math.round((a % 86400) / 3600)
  return `${sign}${h ? `${d}d ${h}h` : `${d}d`}`
}

// Absolute reading, in the metric's own unit.
function fmtValue(kind: Kind, v: number): string {
  switch (kind) {
    case 'pct':
      return `${Math.round(v * 100)}%`
    case 'duration':
      return fmtDuration(v)
    case 'bits':
      return `${v.toFixed(2)} bit`
    case 'scalar':
      return v.toFixed(2)
    case 'delta':
      return `${v >= 0 ? '+' : ''}${v.toFixed(3)}`
    default:
      return String(v)
  }
}

// Signed change in the metric's unit (the 30-day delta chip).
function fmtChange(kind: Kind, d: number): string {
  switch (kind) {
    case 'pct':
      return `${d >= 0 ? '+' : ''}${Math.round(d * 100)} pt`
    case 'duration':
      return `${d >= 0 ? '+' : ''}${fmtDuration(d)}`
    case 'bits':
      return `${d >= 0 ? '+' : ''}${d.toFixed(2)} bit`
    case 'scalar':
      return `${d >= 0 ? '+' : ''}${d.toFixed(2)}`
    case 'delta':
      return `${d >= 0 ? '+' : ''}${d.toFixed(3)}`
    default:
      return String(d)
  }
}

function arrow(d: number): string {
  return d > 0 ? '▲' : d < 0 ? '▼' : '→'
}

// Tiny dependency-free sparkline + an optional dashed floor reference
// line. Nothing to render until at least two daily snapshots exist.
function Sparkline({ points, floor }: { points: number[]; floor?: number }) {
  if (points.length < 2) return null
  const w = 132
  const h = 28
  const pad = 2
  const all = floor != null ? [...points, floor] : points
  const min = Math.min(...all)
  const max = Math.max(...all)
  const span = max - min || 1
  const yOf = (v: number) => h - pad - ((v - min) / span) * (h - 2 * pad)
  const step = (w - 2 * pad) / (points.length - 1)
  const coords = points
    .map((v, i) => `${(pad + i * step).toFixed(1)},${yOf(v).toFixed(1)}`)
    .join(' ')
  return (
    <svg
      className="ghealth__spark"
      width={w}
      height={h}
      viewBox={`0 0 ${w} ${h}`}
      aria-hidden="true"
    >
      {floor != null && (
        <line
          x1={pad}
          x2={w - pad}
          y1={yOf(floor)}
          y2={yOf(floor)}
          stroke="currentColor"
          strokeWidth="0.75"
          strokeDasharray="3 3"
          opacity="0.4"
        />
      )}
      <polyline points={coords} fill="none" stroke="currentColor" strokeWidth="1.5" />
    </svg>
  )
}

function SensorCard({
  keyName,
  metric,
  series,
  onOpen,
}: {
  keyName: string
  metric: Metric
  series: number[]
  onOpen: () => void
}) {
  const { t } = useTranslation()
  const meta = META[keyName]
  const v = metric.value
  const floor = metric.floor
  // Health sensors alarm BELOW the floor; a budget gauge alarms AT/OVER the
  // cap (being under is good). Either way the muted-red styling reuses
  // ghealth__value--below.
  const alarm = v != null && floor != null && (meta.gauge ? v >= floor : v < floor)
  const hasTrend = series.length >= 2
  const change = hasTrend ? series[series.length - 1] - series[0] : null
  const lo = hasTrend ? Math.min(...series) : null
  const hi = hasTrend ? Math.max(...series) : null
  const dir = t(`gardenHealth.dir.${meta.dir}`)

  return (
    <section
      className="ghealth__card ghealth__card--clickable"
      role="button"
      tabIndex={0}
      title={t('gardenHealth.viewTrend')}
      onClick={onOpen}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          onOpen()
        }
      }}
    >
      <h2 className="ghealth__label">{t(`gardenHealth.sensor.${keyName}.label`)}</h2>

      {v == null ? (
        <p className="ghealth__value ghealth__value--empty">{t('gardenHealth.noReading')}</p>
      ) : meta.trendLead && change != null ? (
        // Absolute value is near-meaningless: the 30-day change leads.
        <>
          <p className="ghealth__value">
            {arrow(change)} {fmtChange(meta.kind, change)}
            <span className="ghealth__sub">{t('gardenHealth.over30')}</span>
          </p>
          <p className="ghealth__abs">
            {t('gardenHealth.now')}: {fmtValue(meta.kind, v)} {'·'} {dir}
          </p>
        </>
      ) : (
        <p className={'ghealth__value' + (alarm ? ' ghealth__value--below' : '')}>
          {meta.kind === 'delta' ? `${arrow(v)} ` : ''}
          {fmtValue(meta.kind, v)}
          <span className="ghealth__dir">{dir}</span>
        </p>
      )}
      {v == null && metric.reason && <p className="ghealth__reason">{metric.reason}</p>}

      {hasTrend && <Sparkline points={series} floor={floor ?? undefined} />}
      {hasTrend && !meta.trendLead && change != null && (
        <p className="ghealth__delta">
          {t('gardenHealth.over30')}: {arrow(change)} {fmtChange(meta.kind, change)}
        </p>
      )}
      {hasTrend && lo != null && hi != null && (
        <p className="ghealth__range">
          {t('gardenHealth.range30')}: {fmtValue(meta.kind, lo)}
          {'–'}
          {fmtValue(meta.kind, hi)}
        </p>
      )}
      {floor != null && (
        <p className="ghealth__floor">
          {t(meta.gauge ? 'gardenHealth.cap' : 'gardenHealth.floor')}: {fmtValue(meta.kind, floor)}
        </p>
      )}
      <p className="ghealth__explain">{t(`gardenHealth.sensor.${keyName}.explain`)}</p>
    </section>
  )
}

// Larger line chart for the drill-down: the metric's value over time with
// the floor drawn as a dashed reference line. Width-responsive (the
// viewBox scales); the axis context is in the caption below, not in the
// SVG, to keep it dependency-free.
function TrendChart({
  points,
  floor,
}: {
  points: { day: string; value: number }[]
  floor?: number
}) {
  const w = 560
  const h = 160
  const padX = 6
  const padT = 12
  const padB = 14
  const vals = points.map((p) => p.value)
  const all = floor != null ? [...vals, floor] : vals
  const min = Math.min(...all)
  const max = Math.max(...all)
  const span = max - min || 1
  const xOf = (i: number) => padX + (i * (w - 2 * padX)) / (points.length - 1)
  const yOf = (v: number) => h - padB - ((v - min) / span) * (h - padT - padB)
  const coords = points.map((p, i) => `${xOf(i).toFixed(1)},${yOf(p.value).toFixed(1)}`).join(' ')
  return (
    <svg
      className="ghealth__drill-chart"
      width="100%"
      height={h}
      viewBox={`0 0 ${w} ${h}`}
      preserveAspectRatio="none"
      aria-hidden="true"
    >
      {floor != null && (
        <line
          x1={padX}
          x2={w - padX}
          y1={yOf(floor)}
          y2={yOf(floor)}
          stroke="currentColor"
          strokeWidth="0.75"
          strokeDasharray="4 4"
          opacity="0.4"
        />
      )}
      <polyline points={coords} fill="none" stroke="currentColor" strokeWidth="1.5" />
    </svg>
  )
}

// Per-metric drill-down (task b820d223): a 90-day trend pulled from the
// dedicated timeseries endpoint (persisted snapshots, no live recompute),
// with the value's own formatting, the floor reference, and the 90-day
// range. Opened by clicking a sensor card.
function MetricDrillDown({
  keyName,
  current,
  onClose,
}: {
  keyName: string
  current: Metric
  onClose: () => void
}) {
  const { t } = useTranslation()
  const meta = META[keyName]
  const [points, setPoints] = useState<{ day: string; value: number }[] | null>(null)

  useEffect(() => {
    let active = true
    void api
      .GET('/garden/health/timeseries', {
        params: { header: workspaceHeader(), query: { days: 90 } },
      })
      .then((r) => {
        if (!active) return
        const snaps = r.data ?? []
        const series = [...snaps].reverse().flatMap((s) => {
          const v = s.metrics?.[keyName]?.value
          return typeof v === 'number' ? [{ day: s.day, value: v }] : []
        })
        setPoints(series)
      })
    return () => {
      active = false
    }
  }, [keyName])

  const floor = current.floor ?? undefined
  const vals = (points ?? []).map((p) => p.value)
  const lo = vals.length ? Math.min(...vals) : null
  const hi = vals.length ? Math.max(...vals) : null
  const now = current.value

  return (
    <div
      className="ghealth__drill"
      role="dialog"
      aria-modal="true"
      aria-label={t(`gardenHealth.sensor.${keyName}.label`)}
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose()
      }}
    >
      <div className="ghealth__drill-panel">
        <header className="ghealth__drill-head">
          <h2>{t(`gardenHealth.sensor.${keyName}.label`)}</h2>
          <button
            type="button"
            className="ghealth__drill-close"
            onClick={onClose}
            aria-label={t('gardenHealth.close')}
          >
            ×
          </button>
        </header>
        {points == null ? (
          <p className="hint">{t('common.loading')}</p>
        ) : points.length < 2 ? (
          <p className="ghealth__drill-empty">{t('gardenHealth.notEnough')}</p>
        ) : (
          <>
            <TrendChart points={points} floor={floor} />
            <p className="ghealth__drill-stats">
              {now != null && (
                <>
                  {t('gardenHealth.now')}: <b>{fmtValue(meta.kind, now)}</b>
                  {' · '}
                </>
              )}
              {lo != null && hi != null && (
                <>
                  {t('gardenHealth.range90')}: {fmtValue(meta.kind, lo)}
                  {'–'}
                  {fmtValue(meta.kind, hi)}
                </>
              )}
              {floor != null && (
                <>
                  {' · '}
                  {t('gardenHealth.floor')}: {fmtValue(meta.kind, floor)}
                </>
              )}
            </p>
            <p className="ghealth__drill-span">
              {points[0].day} → {points[points.length - 1].day}
            </p>
          </>
        )}
        <p className="ghealth__explain">{t(`gardenHealth.sensor.${keyName}.explain`)}</p>
      </div>
    </div>
  )
}

// "What changed" timeline (ADR-0035 §84, task d0bada67): discrete events
// that may explain a shift in the sensors above -- a classifier bump or a
// bulk corpus edit -- so a reading is interpreted, not guessed. Derived
// live from the audit + feedback streams; empty until something actually
// changed. "Show, never judge": facts, never a verdict.
function WhatChangedTimeline() {
  const { t, i18n } = useTranslation()
  const [events, setEvents] = useState<HealthEvent[] | null | undefined>(undefined)

  useEffect(() => {
    let active = true
    void api
      .GET('/garden/health/events', {
        params: { header: workspaceHeader(), query: { days: 90 } },
      })
      .then((r) => {
        if (active) setEvents(r.data ?? null)
      })
    return () => {
      active = false
    }
  }, [])

  const describe = (e: HealthEvent): string => {
    if (e.kind === 'classifier_version') {
      const version = String((e.detail as { version?: unknown }).version ?? '?')
      return t('gardenHealth.timeline.classifierVersion', { version })
    }
    const d = e.detail as { action?: unknown; count?: unknown }
    const action = String(d.action ?? '')
    const count = typeof d.count === 'number' ? d.count : 0
    return t(`gardenHealth.timeline.action.${action}`, { count, defaultValue: `${count}` })
  }

  const fmtDay = (iso: string): string =>
    new Date(iso).toLocaleDateString(i18n.language, {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
    })

  return (
    <section className="ghealth__timeline">
      <h2 className="ghealth__timeline-head">{t('gardenHealth.timeline.title')}</h2>
      <p className="ghealth__timeline-intro">{t('gardenHealth.timeline.intro')}</p>
      {events === undefined && <p className="hint">{t('common.loading')}</p>}
      {events === null && <p className="error">{t('gardenHealth.loadError')}</p>}
      {events && events.length === 0 && (
        <p className="ghealth__timeline-empty">{t('gardenHealth.timeline.empty')}</p>
      )}
      {events && events.length > 0 && (
        <ul className="ghealth__timeline-list">
          {events.map((e, i) => (
            <li key={`${e.kind}-${e.at}-${i}`} className="ghealth__timeline-item">
              <time className="ghealth__timeline-date" dateTime={e.at}>
                {fmtDay(e.at)}
              </time>
              <span className="ghealth__timeline-label">{describe(e)}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

// A feature key is type-prefixed (``tag:<id>`` / ``link_target:<id>``);
// split it into a type tag + the raw id (the SPA owns label rendering).
function featureParts(key: string): { typeKey: 'tag' | 'link' | null; id: string } {
  if (key.startsWith('tag:')) return { typeKey: 'tag', id: key.slice(4) }
  if (key.startsWith('link_target:'))
    return { typeKey: 'link', id: key.slice('link_target:'.length) }
  return { typeKey: null, id: key }
}

// Signed horizontal bars for the 30-day prior drift: right/green = the
// preference strengthened, left/red = weakened, around a zero baseline.
// Dependency-free SVG, same idiom as Sparkline/TrendChart.
function DriftBars({ items }: { items: Drift[] }) {
  const w = 260
  const rowH = 22
  const pad = 4
  const h = items.length * rowH
  const maxAbs = Math.max(...items.map((d) => Math.abs(d.delta)), 1e-4)
  const xOf = (v: number) => pad + ((v + maxAbs) / (2 * maxAbs)) * (w - 2 * pad)
  const x0 = xOf(0)
  return (
    <svg
      className="ghealth__drift-chart"
      width="100%"
      height={h}
      viewBox={`0 0 ${w} ${h}`}
      preserveAspectRatio="none"
      aria-hidden="true"
    >
      <line x1={x0} x2={x0} y1={0} y2={h} stroke="currentColor" strokeWidth="0.5" opacity="0.3" />
      {items.map((d, i) => {
        const xv = xOf(d.delta)
        const y = i * rowH + 5
        return (
          <rect
            key={d.feature_key}
            x={Math.min(x0, xv)}
            y={y}
            width={Math.max(1, Math.abs(xv - x0))}
            height={rowH - 10}
            fill={d.delta >= 0 ? '#4a8f6b' : '#b0553f'}
          />
        )
      })}
    </svg>
  )
}

// Learning signals (ADR-0037 telemetry, task 8aff04b9): the user's own
// reject-hotspots (suggestions they decline most, to mute at the source)
// + the biggest 30-day prior shifts. Read-only, "show, never judge";
// the user's own history only (no cross-user comparison, ADR-0037). Its
// own fetch, like WhatChangedTimeline; empty/error states are explicit.
function LearningPanel() {
  const { t } = useTranslation()
  const [data, setData] = useState<Telemetry | null | undefined>(undefined)

  useEffect(() => {
    let active = true
    void api
      .GET('/garden/learning/telemetry', {
        params: {
          header: workspaceHeader(),
          query: { reject_days: 90, drift_days: 30, limit: 10 },
        },
      })
      .then((r) => {
        if (active) setData(r.data ?? null)
      })
    return () => {
      active = false
    }
  }, [])

  const hotspots = data?.reject_hotspots ?? []
  const drift = data?.drift ?? []

  return (
    <section className="ghealth__learning">
      <h2 className="ghealth__learning-head">{t('gardenHealth.learning.title')}</h2>
      <p className="ghealth__learning-intro">{t('gardenHealth.learning.intro')}</p>
      {data === undefined && <p className="hint">{t('common.loading')}</p>}
      {data === null && <p className="error">{t('gardenHealth.learning.loadError')}</p>}
      {data && (
        <div className="ghealth__learning-cols">
          <div className="ghealth__hotspots">
            <h3 className="ghealth__learning-sub-head">
              {t('gardenHealth.learning.rejectTitle')}
            </h3>
            <p className="ghealth__learning-sub">{t('gardenHealth.learning.rejectIntro')}</p>
            {hotspots.length === 0 ? (
              <p className="ghealth__learning-empty">{t('gardenHealth.learning.rejectEmpty')}</p>
            ) : (
              <ul className="ghealth__hotspot-list">
                {hotspots.map((hsp) => {
                  const f = featureParts(hsp.feature_key)
                  return (
                    <li key={hsp.feature_key} className="ghealth__hotspot-item">
                      {f.typeKey && (
                        <span className="ghealth__hotspot-type">
                          {t(`gardenHealth.learning.type.${f.typeKey}`)}
                        </span>
                      )}
                      <code className="ghealth__hotspot-id">{f.id.slice(0, 8)}</code>
                      <span className="ghealth__hotspot-count">
                        {t('gardenHealth.learning.declined', { count: hsp.declines })}
                      </span>
                    </li>
                  )
                })}
              </ul>
            )}
          </div>
          <div className="ghealth__drift">
            <h3 className="ghealth__learning-sub-head">{t('gardenHealth.learning.driftTitle')}</h3>
            <p className="ghealth__learning-sub">{t('gardenHealth.learning.driftIntro')}</p>
            {drift.length === 0 ? (
              <p className="ghealth__learning-empty">{t('gardenHealth.learning.driftEmpty')}</p>
            ) : (
              <>
                <DriftBars items={drift} />
                <ul className="ghealth__drift-list">
                  {drift.map((d) => {
                    const f = featureParts(d.feature_key)
                    return (
                      <li key={d.feature_key} className="ghealth__drift-item">
                        {f.typeKey && (
                          <span className="ghealth__hotspot-type">
                            {t(`gardenHealth.learning.type.${f.typeKey}`)}
                          </span>
                        )}
                        <code className="ghealth__hotspot-id">{f.id.slice(0, 8)}</code>
                        <span className="ghealth__drift-delta">
                          {d.before.toFixed(2)} {'→'} {d.after.toFixed(2)} (
                          {d.delta >= 0 ? '+' : ''}
                          {d.delta.toFixed(2)})
                        </span>
                      </li>
                    )
                  })}
                </ul>
              </>
            )}
          </div>
        </div>
      )}
    </section>
  )
}

export function GardenHealthRoute() {
  const { t } = useTranslation()
  const [data, setData] = useState<Health | null | undefined>(undefined)
  const [drill, setDrill] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    void api
      .GET('/garden/health', { params: { header: workspaceHeader() } })
      .then((r) => {
        if (active) setData(r.data ?? null)
      })
    return () => {
      active = false
    }
  }, [])

  const seriesFor = (key: string): number[] =>
    data
      ? [...data.trend]
          .reverse()
          .map((s) => s.metrics?.[key]?.value)
          .filter((x): x is number => typeof x === 'number')
      : []

  const live = ORDER.filter((k) => data?.metrics[k] && !META[k].blocked)
  const pending = ORDER.filter((k) => data?.metrics[k] && META[k].blocked)

  return (
    <div className="ghealth">
      <header className="ghealth__head">
        <h1>{t('gardenHealth.title')}</h1>
        <Link to="/garden" className="ghealth__back">
          {t('gardenHealth.back')}
        </Link>
      </header>
      <p className="ghealth__intro">{t('gardenHealth.intro')}</p>

      {data === undefined && <p className="hint">{t('common.loading')}</p>}
      {data === null && <p className="error">{t('gardenHealth.loadError')}</p>}

      {data && (
        <>
          <div className="ghealth__grid">
            {live.map((key) => (
              <SensorCard
                key={key}
                keyName={key}
                metric={data.metrics[key]!}
                series={seriesFor(key)}
                onOpen={() => setDrill(key)}
              />
            ))}
          </div>

          {pending.length > 0 && (
            <section className="ghealth__pending">
              <h2 className="ghealth__pending-head">{t('gardenHealth.pendingTitle')}</h2>
              <p className="ghealth__pending-intro">{t('gardenHealth.pendingIntro')}</p>
              <ul className="ghealth__pending-list">
                {pending.map((key) => (
                  <li key={key} className="ghealth__pending-item">
                    <span className="ghealth__pending-label">
                      {t(`gardenHealth.sensor.${key}.label`)}
                    </span>
                    <span className="ghealth__pending-explain">
                      {t(`gardenHealth.sensor.${key}.explain`)}
                    </span>
                    {data.metrics[key]?.reason && (
                      <span className="ghealth__pending-reason">
                        {data.metrics[key]!.reason}
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            </section>
          )}

          <WhatChangedTimeline />

          <LearningPanel />

          {drill && data.metrics[drill] && (
            <MetricDrillDown
              keyName={drill}
              current={data.metrics[drill]!}
              onClose={() => setDrill(null)}
            />
          )}
        </>
      )}
    </div>
  )
}
