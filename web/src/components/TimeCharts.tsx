import { useTranslation } from 'react-i18next'
import { hhmm, hhmmss } from '../lib/time'
import type { ChartSeries, DayBucket } from './timeChartColors'

// The donut and the per-day histogram are ONE instrument: they take the
// same ordered `series` array and the same `colorOf` map (built once by
// useColorOf in ./timeChartColors), so a colour means the same entity in
// both charts and a filter that drops a bucket never repaints the
// survivors. Neither chart ever derives a colour from its own loop index.
// The palette itself, and the proof that its steps stay apart under CVD
// in both themes, live in that module.

const n2 = (v: number): number => Math.round(v * 100) / 100

/** The legend for BOTH charts: swatch + label + total + share. It is the
 * dependable identity channel (colour alone is never the only cue), and
 * it doubles as the "+N" bucket's direct label. */
export function ChartLegend({
  id,
  series,
  colorOf,
}: {
  id: string
  series: ChartSeries[]
  colorOf: (key: string) => string
}) {
  const { t } = useTranslation()
  const total = series.reduce((s, d) => s + d.secs, 0) || 1
  return (
    <ul className="pielegend" id={id} aria-label={t('time.chartsLegend')}>
      {series.map((d) => (
        <li key={d.key}>
          <span className="pieswatch" style={{ background: colorOf(d.key) }} />
          <span className="grow">{d.label}</span>
          <span className="muted">
            {hhmmss(d.secs)} · {Math.round((d.secs / total) * 100)}%
          </span>
        </li>
      ))}
    </ul>
  )
}

/** Dependency-free SVG donut (stroke-dasharray technique) of the time
 * distribution per bucket, rendered beside the shared legend. */
export function Donut({
  series,
  colorOf,
  legendId,
}: {
  series: ChartSeries[]
  colorOf: (key: string) => string
  legendId: string
}) {
  const { t } = useTranslation()
  const total = series.reduce((s, d) => s + d.secs, 0) || 1
  const r = 42
  const C = 2 * Math.PI * r
  // Functional prefix-sums: each slice starts where the prior ones
  // ended. No mutable accumulator (React Compiler forbids reassigning
  // an outer variable across the map closure during render).
  const fracs = series.map((d) => d.secs / total)
  const offsets = fracs.map((_, i) =>
    fracs.slice(0, i).reduce((s, x) => s + x, 0),
  )
  return (
    <div className="timepie">
      <svg
        viewBox="0 0 120 120"
        width="150"
        height="150"
        role="img"
        aria-label={t('time.donutAria')}
        aria-describedby={legendId}
      >
        <g transform="rotate(-90 60 60)">
          {series.map((d, i) => (
            <circle
              key={d.key}
              className="timepie__slice"
              cx="60"
              cy="60"
              r={r}
              fill="none"
              stroke={colorOf(d.key)}
              strokeWidth="16"
              strokeDasharray={`${fracs[i] * C} ${C}`}
              strokeDashoffset={-offsets[i] * C}
            >
              <title>
                {`${d.label} · ${hhmm(d.secs)} · ${Math.round(fracs[i] * 100)}%`}
              </title>
            </circle>
          ))}
        </g>
      </svg>
      <ChartLegend id={legendId} series={series} colorOf={colorOf} />
    </div>
  )
}

// --- per-day histogram -----------------------------------------------

const PLOT_H = 180 // plot band only; the axis band is added below it
const X_BAND = 22 // room for the date labels, so nothing is clipped
const PAD_T = 8 // headroom above the tallest column
const AXIS_W = 40 // left gutter for the hour ticks
const PAD_R = 20 // keeps the last date label inside the viewBox
const SLOT_MAX = 24 // one day's horizontal slot at a comfortable width
const SLOT_MIN = 10 // floor for long ranges (the wrapper scrolls instead)
const TARGET_W = 960 // width we try to fit before shrinking the slots
const GAP = 2 // the surface gap: between stacked segments AND columns
const CAP_R = 4 // rounded top on the topmost segment only
const LABEL_W = 36 // pitch a "DD/MM" needs at 10px to clear its neighbour

// Nice hour steps for the y axis. Whole hours wherever possible; the 0.5
// step only kicks in for very short days, where "30m" is the honest tick.
const STEPS_H = [0.5, 1, 2, 3, 4, 6, 8, 12, 24]

function tickLabel(h: number): string {
  if (h === 0) return '0'
  if (h < 1) return `${Math.round(h * 60)}m`
  if (Number.isInteger(h)) return `${h}h`
  return `${Math.floor(h)}h${String(Math.round((h % 1) * 60)).padStart(2, '0')}`
}

// A rounded-top, square-bottom column cap: the data-end is rounded, the
// baseline end is not.
function capPath(x: number, y: number, w: number, h: number): string {
  const r = Math.max(0, Math.min(CAP_R, w / 2, h))
  const b = n2(y + h)
  return (
    `M${n2(x)} ${b}L${n2(x)} ${n2(y + r)}` +
    `A${n2(r)} ${n2(r)} 0 0 1 ${n2(x + r)} ${n2(y)}` +
    `L${n2(x + w - r)} ${n2(y)}` +
    `A${n2(r)} ${n2(r)} 0 0 1 ${n2(x + w)} ${n2(y + r)}` +
    `L${n2(x + w)} ${b}Z`
  )
}

/** Stacked column chart of tracked time per calendar day.
 *
 * Every day of the selected range gets a column, including days with no
 * time: an empty slot has to read as "no work", not vanish. Segments
 * stack in the SHARED series order and wear the SHARED colour map, so a
 * hue means the same entity here and in the donut above. */
export function DayBars({
  days,
  series,
  colorOf,
  legendId,
}: {
  days: DayBucket[]
  series: ChartSeries[]
  colorOf: (key: string) => string
  legendId?: string
}) {
  const { t, i18n } = useTranslation()

  const cols = days.map((d) => {
    const parts = series
      .map((s) => ({
        key: s.key,
        label: s.label,
        secs: Math.max(0, d.parts[s.key] ?? 0),
      }))
      .filter((p) => p.secs > 0)
    // Prefix sums again, for the same React Compiler reason as the donut.
    const below = parts.map((_, i) =>
      parts.slice(0, i).reduce((a, p) => a + p.secs, 0),
    )
    const total = parts.reduce((a, p) => a + p.secs, 0)
    return { day: d.day, parts, below, total }
  })
  const maxSecs = cols.reduce((m, c) => Math.max(m, c.total), 0)

  const fmtDay = (ymd: string): string => {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(ymd)
    if (!m) return ymd
    return new Date(
      Number(m[1]),
      Number(m[2]) - 1,
      Number(m[3]),
      12,
    ).toLocaleDateString(i18n.language, { day: '2-digit', month: '2-digit' })
  }
  const fmtDayLong = (ymd: string): string => {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(ymd)
    if (!m) return ymd
    return new Date(
      Number(m[1]),
      Number(m[2]) - 1,
      Number(m[3]),
      12,
    ).toLocaleDateString(i18n.language, {
      weekday: 'short',
      day: '2-digit',
      month: 'short',
    })
  }

  if (cols.length === 0 || maxSecs <= 0) {
    return <p className="hint">{t('time.byDayEmpty')}</p>
  }

  // Fixed pixel geometry, then scroll. A percentage width would let a
  // 365-day range squeeze columns to sub-pixel slivers; instead the slot
  // shrinks to a legible floor and the WRAPPER scrolls, so the page body
  // never gains a horizontal scrollbar.
  const slot = Math.max(
    SLOT_MIN,
    Math.min(SLOT_MAX, Math.floor(TARGET_W / cols.length)),
  )
  const barW = Math.max(4, slot - GAP)
  const width = AXIS_W + cols.length * slot + PAD_R
  const height = PAD_T + PLOT_H + X_BAND
  const baseY = PAD_T + PLOT_H

  const maxH = maxSecs / 3600
  const step = STEPS_H.find((s) => maxH <= s * 5) ?? Math.ceil(maxH / 5)
  const topH = Math.max(step, Math.ceil(maxH / step) * step)
  const ticks = Array.from({ length: Math.round(topH / step) + 1 }, (_, k) =>
    n2(k * step),
  )
  const yOf = (secs: number): number => baseY - (secs / (topH * 3600)) * PLOT_H

  // Thin the date labels so they can never collide, whatever the range.
  // Two constraints, and BOTH have to hold: at most ~12 labels across the
  // axis, AND enough pixels between two labelled columns to fit a
  // "DD/MM". The count rule alone is not enough — a 7-day range keeps
  // every label (7 < 12) on 24px slots, and a ~30px label on a 24px pitch
  // overprints its neighbour.
  const stride = Math.max(
    1,
    Math.ceil(cols.length / 12),
    Math.ceil(LABEL_W / slot),
  )

  return (
    <figure className="daybars">
      <div className="daybars__scroll">
        <svg
          width={width}
          height={height}
          viewBox={`0 0 ${width} ${height}`}
          role="img"
          aria-label={t('time.byDayAria')}
          aria-describedby={legendId}
        >
          {/* Column hit areas first, UNDER the grid: hovering anywhere in
              a day's slot reports that day's total, and the highlight
              band never hides the gridlines drawn on top of it. */}
          <g className="daybars__hits">
            {cols.map((c, i) => (
              <rect
                key={c.day}
                className="daybars__hit"
                x={AXIS_W + i * slot}
                y={PAD_T}
                width={slot}
                height={PLOT_H}
              >
                <title>{`${fmtDayLong(c.day)} · ${hhmm(c.total)}`}</title>
              </rect>
            ))}
          </g>

          <g className="daybars__grid">
            {ticks.map((h) => (
              <g key={h}>
                <line
                  x1={AXIS_W}
                  x2={width - PAD_R}
                  y1={n2(yOf(h * 3600))}
                  y2={n2(yOf(h * 3600))}
                />
                <text
                  className="daybars__tick"
                  x={AXIS_W - 6}
                  y={n2(yOf(h * 3600) + 3)}
                  textAnchor="end"
                >
                  {tickLabel(h)}
                </text>
              </g>
            ))}
          </g>

          <g className="daybars__stacks">
            {cols.map((c, i) => (
              <g key={c.day}>
                {c.parts.map((p, k) => {
                  const x = AXIS_W + i * slot + (slot - barW) / 2
                  const yBot = yOf(c.below[k])
                  const yTop = yOf(c.below[k] + p.secs)
                  const isTop = k === c.parts.length - 1
                  // The 2px surface gap is carved out of the TOP of every
                  // segment but the last, so the surface does the
                  // separating and no segment needs a stroke around it.
                  const h = isTop
                    ? yBot - yTop
                    : Math.max(0.75, yBot - yTop - GAP)
                  const y = yBot - h
                  const title = `${p.label} · ${hhmm(p.secs)}`
                  return isTop ? (
                    <path
                      key={p.key}
                      className="daybars__seg"
                      d={capPath(x, y, barW, h)}
                      fill={colorOf(p.key)}
                    >
                      <title>{title}</title>
                    </path>
                  ) : (
                    <rect
                      key={p.key}
                      className="daybars__seg"
                      x={n2(x)}
                      y={n2(y)}
                      width={n2(barW)}
                      height={n2(h)}
                      fill={colorOf(p.key)}
                    >
                      <title>{title}</title>
                    </rect>
                  )
                })}
              </g>
            ))}
          </g>

          <line
            className="daybars__axis"
            x1={AXIS_W}
            x2={width - PAD_R}
            y1={baseY}
            y2={baseY}
          />

          <g className="daybars__xlabels">
            {cols.map((c, i) =>
              i % stride === 0 ? (
                <text
                  key={c.day}
                  x={n2(AXIS_W + i * slot + slot / 2)}
                  y={baseY + 14}
                  textAnchor="middle"
                >
                  {fmtDay(c.day)}
                </text>
              ) : null,
            )}
          </g>
        </svg>
      </div>
      {/* The hover tooltips enhance, they never gate — but the text
          fallback is NOT repeated here: TimeRoute renders the same per-day
          numbers as a real <table> right below this figure (the billing
          view). An sr-only restatement on top of that would read every day
          out twice. */}
    </figure>
  )
}
