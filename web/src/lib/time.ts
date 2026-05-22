// Shared elapsed/time formatting (timer chips, running indicator).
export function hms(sec: number): string {
  const s = Math.max(0, Math.floor(sec))
  return `${Math.floor(s / 3600)}:${String(
    Math.floor((s % 3600) / 60),
  ).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`
}

export function elapsedSec(startedAtIso: string, nowMs: number): number {
  return (nowMs - new Date(startedAtIso).getTime()) / 1000
}

// Compact, locale-driven relative time ("3 hr. ago", "ieri") for recency
// surfaces (Recent-tasks widget). Uses Intl.RelativeTimeFormat keyed on the
// active i18n language, so there's no hardcoded copy to translate. Anything
// older than ~30 days degrades to an absolute date, where "N days ago" stops
// being legible.
export function relTime(iso: string, locale: string, nowMs: number = Date.now()): string {
  const then = new Date(iso).getTime()
  if (!Number.isFinite(then)) return ''
  const sec = Math.round((then - nowMs) / 1000) // negative = past
  const rtf = new Intl.RelativeTimeFormat(locale, { numeric: 'auto', style: 'short' })
  if (Math.abs(sec) < 60) return rtf.format(sec, 'second')
  const min = Math.round(sec / 60)
  if (Math.abs(min) < 60) return rtf.format(min, 'minute')
  const hr = Math.round(sec / 3600)
  if (Math.abs(hr) < 24) return rtf.format(hr, 'hour')
  const day = Math.round(sec / 86400)
  if (Math.abs(day) < 30) return rtf.format(day, 'day')
  return new Date(iso).toLocaleDateString(locale, { dateStyle: 'medium' })
}
