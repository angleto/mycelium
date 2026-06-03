import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api, workspaceHeader } from '../api/client'
import type { components } from '../api/schema'

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

type Kind = 'pct' | 'duration' | 'bits' | 'scalar' | 'delta'
type Dir = 'higher' | 'lower' | 'trend' | 'signed'

interface Meta {
  kind: Kind
  dir: Dir
  // Absolute value is near-meaningless: lead with the 30-day change.
  trendLead?: boolean
  // Not yet wired to a data source: listed apart, never a faked empty.
  blocked?: boolean
}

// Display order (ADR-0035), with per-metric presentation.
const META: Record<string, Meta> = {
  accept_rate_classify_7d: { kind: 'pct', dir: 'higher' },
  accept_rate_classify_30d: { kind: 'pct', dir: 'higher' },
  time_to_first_link: { kind: 'duration', dir: 'lower' },
  tag_entropy_local: { kind: 'bits', dir: 'higher' },
  leiden_modularity: { kind: 'scalar', dir: 'trend', trendLead: true },
  density_delta_7d: { kind: 'delta', dir: 'signed' },
  recall_at_k: { kind: 'pct', dir: 'higher', blocked: true },
  fungal_lag: { kind: 'duration', dir: 'lower', blocked: true },
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
}: {
  keyName: string
  metric: Metric
  series: number[]
}) {
  const { t } = useTranslation()
  const meta = META[keyName]
  const v = metric.value
  const floor = metric.floor
  const below = v != null && floor != null && v < floor
  const hasTrend = series.length >= 2
  const change = hasTrend ? series[series.length - 1] - series[0] : null
  const lo = hasTrend ? Math.min(...series) : null
  const hi = hasTrend ? Math.max(...series) : null
  const dir = t(`gardenHealth.dir.${meta.dir}`)

  return (
    <section className="ghealth__card">
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
        <p className={'ghealth__value' + (below ? ' ghealth__value--below' : '')}>
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
          {t('gardenHealth.floor')}: {fmtValue(meta.kind, floor)}
        </p>
      )}
      <p className="ghealth__explain">{t(`gardenHealth.sensor.${keyName}.explain`)}</p>
    </section>
  )
}

export function GardenHealthRoute() {
  const { t } = useTranslation()
  const [data, setData] = useState<Health | null | undefined>(undefined)

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
        </>
      )}
    </div>
  )
}
