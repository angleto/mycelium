// Format an estimate in hours as a compact label: sub-hour values as
// minutes (0.5 -> "30m"), whole hours as "Nh", else one decimal.
export function formatHours(h: number): string {
  if (h <= 0) return ''
  if (h < 1) return `${Math.round(h * 60)}m`
  return Number.isInteger(h) ? `${h}h` : `${h.toFixed(2).replace(/0+$/, '')}h`
}
