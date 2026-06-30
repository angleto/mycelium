// Format a UTC ISO timestamp in a target IANA timezone (the client's
// tz when set, else the browser's). Used by the time report/entries so
// corrections and totals read in the client's local time.
export function fmtDateTime(iso: string, tz?: string | null): string {
  const d = new Date(iso)
  try {
    return d.toLocaleString([], {
      dateStyle: 'medium',
      timeStyle: 'short',
      ...(tz ? { timeZone: tz } : {}),
    })
  } catch {
    // Invalid IANA tz → fall back to local.
    return d.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' })
  }
}

export function fmtClock(iso: string | null | undefined, tz?: string | null): string {
  if (!iso) return '…'
  const d = new Date(iso)
  try {
    return d.toLocaleTimeString([], {
      hour: '2-digit',
      minute: '2-digit',
      ...(tz ? { timeZone: tz } : {}),
    })
  } catch {
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  }
}

// For <input type="datetime-local"> (no tz; local wall time of the
// browser). Round-trips an ISO to the value the control expects.
export function toLocalInput(iso: string | null | undefined): string {
  if (!iso) return ''
  const d = new Date(iso)
  const pad = (n: number) => String(n).padStart(2, '0')
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
    `T${pad(d.getHours())}:${pad(d.getMinutes())}`
  )
}

export function fromLocalInput(v: string): string {
  // datetime-local has no zone; interpret as browser-local, emit ISO.
  return new Date(v).toISOString()
}
