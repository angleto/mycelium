import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api, workspaceHeader } from '../api/client'
import type { components } from '../api/schema'

// Garden health sensors (ADR-0035): one card per structural metric, with
// its value, health floor, a 30-day sparkline, and an inline plain-language
// explanation. "Show, never judge": below the floor the value renders in
// muted red, above it in default text -- a reading, not a verdict. No
// traffic lights, no single score.

type Health = components['schemas']['GardenHealthOut']
type Metric = components['schemas']['GardenHealthMetricOut']

// Display order (ADR-0035). Labels + explanations come from the i18n
// catalog (no hardcoded UI copy).
const SENSORS = [
  'accept_rate_classify_7d',
  'accept_rate_classify_30d',
  'time_to_first_link',
  'tag_entropy_local',
  'leiden_modularity',
  'fungal_lag',
  'density_delta_7d',
] as const

// Tiny dependency-free sparkline: a single normalised polyline. Nothing
// to render until at least two daily snapshots exist.
function Sparkline({ points }: { points: number[] }) {
  if (points.length < 2) return null
  const w = 132
  const h = 28
  const pad = 2
  const min = Math.min(...points)
  const max = Math.max(...points)
  const span = max - min || 1
  const step = (w - 2 * pad) / (points.length - 1)
  const coords = points
    .map((v, i) => {
      const x = pad + i * step
      const y = h - pad - ((v - min) / span) * (h - 2 * pad)
      return `${x.toFixed(1)},${y.toFixed(1)}`
    })
    .join(' ')
  return (
    <svg
      className="ghealth__spark"
      width={w}
      height={h}
      viewBox={`0 0 ${w} ${h}`}
      aria-hidden="true"
    >
      <polyline points={coords} fill="none" stroke="currentColor" strokeWidth="1.5" />
    </svg>
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
        <div className="ghealth__grid">
          {SENSORS.map((key) => {
            const m: Metric | undefined = data.metrics[key]
            if (!m) return null
            // This sensor's values across the trend, oldest -> newest.
            const series = [...data.trend]
              .reverse()
              .map((s) => s.metrics?.[key]?.value)
              .filter((v): v is number => typeof v === 'number')
            const below = m.value != null && m.floor != null && m.value < m.floor
            return (
              <section key={key} className="ghealth__card">
                <h2 className="ghealth__label">{t(`gardenHealth.sensor.${key}.label`)}</h2>
                {m.value != null ? (
                  <p
                    className={'ghealth__value' + (below ? ' ghealth__value--below' : '')}
                  >
                    {m.value}
                  </p>
                ) : (
                  <p className="ghealth__value ghealth__value--empty">
                    {t('gardenHealth.noReading')}
                  </p>
                )}
                {m.floor != null && (
                  <p className="ghealth__floor">
                    {t('gardenHealth.floor')}: {m.floor}
                  </p>
                )}
                <Sparkline points={series} />
                <p className="ghealth__explain">{t(`gardenHealth.sensor.${key}.explain`)}</p>
                {m.value == null && m.reason && <p className="ghealth__reason">{m.reason}</p>}
              </section>
            )
          })}
        </div>
      )}
    </div>
  )
}
