// Period selector helpers (#65). Shared by the Time report and the
// Invoices "bill from tracked time" picker. Each call returns
// { from, to, label, prevAnchor, nextAnchor, prevFrom, prevTo } so a
// consumer can render navigation + a previous-period diff fetch. The
// anchor is a Date stamped at noon local to avoid DST edge cases when
// stepping by 1 month / 1 week.
export type Period = 'day' | 'week' | 'month' | 'custom'

export function pad2(n: number): string {
  return String(n).padStart(2, '0')
}

export function ymdLocal(d: Date): string {
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`
}

// Hard ceiling on enumerateDays' output. `<input type="date">` happily
// yields a year like 0202 on a typo, and an unbounded day walk over that
// range locks the tab. ~10 years is far past any range the Time view can
// plot legibly, so truncating is strictly better than hanging.
const MAX_ENUMERATED_DAYS = 3700

// Parse a "YYYY-MM-DD" as a LOCAL date. `new Date('2026-07-31')` is
// parsed as UTC midnight, which lands on the 30th west of Greenwich —
// so build the Date from parts, at noon, the same DST-safe convention
// periodRange uses.
function parseYmdLocal(s: string): Date | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s)
  if (!m) return null
  const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]), 12)
  return Number.isFinite(d.getTime()) ? d : null
}

/** Every calendar day in `[from, to]`, inclusive, as YYYY-MM-DD.
 *
 * Pure: the per-day histogram zero-fills against this, so a day with no
 * tracked time still gets a column and reads as "no work" instead of
 * being silently skipped. An inverted or unparseable range yields `[]`
 * (the caller renders its empty state) rather than throwing. */
export function enumerateDays(from: string, to: string): string[] {
  const start = parseYmdLocal(from)
  const end = parseYmdLocal(to)
  if (!start || !end || end < start) return []
  const out: string[] = []
  const cur = new Date(start)
  while (cur <= end && out.length < MAX_ENUMERATED_DAYS) {
    out.push(ymdLocal(cur))
    cur.setDate(cur.getDate() + 1)
  }
  return out
}

export interface PeriodRange {
  from: string
  to: string
  label: string
  prevAnchor: Date
  nextAnchor: Date
  prevFrom: string
  prevTo: string
}

export function periodRange(period: Period, anchor: Date): PeriodRange {
  const a = new Date(anchor.getFullYear(), anchor.getMonth(), anchor.getDate(), 12)
  if (period === 'day') {
    const from = ymdLocal(a)
    const to = ymdLocal(a)
    const label = a.toLocaleDateString(undefined, {
      weekday: 'short',
      day: '2-digit',
      month: 'short',
      year: 'numeric',
    })
    const prev = new Date(a)
    prev.setDate(prev.getDate() - 1)
    const next = new Date(a)
    next.setDate(next.getDate() + 1)
    return {
      from,
      to,
      label,
      prevAnchor: prev,
      nextAnchor: next,
      prevFrom: ymdLocal(prev),
      prevTo: ymdLocal(prev),
    }
  }
  if (period === 'week') {
    // ISO-ish: week starts on Monday. getDay() = 0 (Sun) .. 6 (Sat).
    const day = a.getDay() === 0 ? 7 : a.getDay()
    const start = new Date(a)
    start.setDate(a.getDate() - (day - 1))
    const end = new Date(start)
    end.setDate(start.getDate() + 6)
    const label = `${start.toLocaleDateString(undefined, { day: '2-digit', month: 'short' })} – ${end.toLocaleDateString(undefined, { day: '2-digit', month: 'short', year: 'numeric' })}`
    const prevStart = new Date(start)
    prevStart.setDate(prevStart.getDate() - 7)
    const prevEnd = new Date(prevStart)
    prevEnd.setDate(prevEnd.getDate() + 6)
    const nextAnchor = new Date(start)
    nextAnchor.setDate(start.getDate() + 7)
    return {
      from: ymdLocal(start),
      to: ymdLocal(end),
      label,
      prevAnchor: prevStart,
      nextAnchor,
      prevFrom: ymdLocal(prevStart),
      prevTo: ymdLocal(prevEnd),
    }
  }
  // month
  const start = new Date(a.getFullYear(), a.getMonth(), 1, 12)
  const end = new Date(a.getFullYear(), a.getMonth() + 1, 0, 12)
  const label = start.toLocaleDateString(undefined, {
    month: 'long',
    year: 'numeric',
  })
  const prevStart = new Date(a.getFullYear(), a.getMonth() - 1, 1, 12)
  const prevEnd = new Date(a.getFullYear(), a.getMonth(), 0, 12)
  const nextAnchor = new Date(a.getFullYear(), a.getMonth() + 1, 1, 12)
  return {
    from: ymdLocal(start),
    to: ymdLocal(end),
    label,
    prevAnchor: prevStart,
    nextAnchor,
    prevFrom: ymdLocal(prevStart),
    prevTo: ymdLocal(prevEnd),
  }
}
