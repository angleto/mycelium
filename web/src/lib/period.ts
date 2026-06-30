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
