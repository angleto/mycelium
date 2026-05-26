import type { TFunction } from 'i18next'

/** Type of a revision field, used to drive both display formatting
 * and equality comparison.
 *
 * - ``date``: server emits ``YYYY-MM-DD`` strings; the SPA can also
 *   hold a ``Date`` object after a fresh fetch
 * - ``datetime``: ISO 8601 with timezone offset on the server,
 *   ``Date``/string on the client side
 * - ``decimal``: server emits Decimal as string (preserves precision);
 *   the SPA can have ``number`` or ``string``
 * - ``integer`` / ``boolean`` / ``string``: scalar
 * - ``json``: anything structural; compared by deep-stable-stringify
 */
export type FieldType =
  | 'date'
  | 'datetime'
  | 'decimal'
  | 'integer'
  | 'boolean'
  | 'string'
  | 'json'

/** Per-kind, per-field type map. The diff and display layers consult
 * this exclusively; ``string`` is the fallback for anything not
 * explicitly typed (so unknown columns render verbatim instead of
 * crashing).
 *
 * Stays in sync with ``_TASK_SNAPSHOT_FIELDS`` /
 * ``_NOTE_SNAPSHOT_FIELDS`` on the server: if a column is added there
 * and shown in the diff, add it here too. A column missing here
 * falls back to ``string`` (verbatim toString), never to a crash. */
export const FIELD_TYPES: Record<
  'task' | 'note',
  Record<string, FieldType>
> = {
  task: {
    title: 'string',
    description: 'string',
    importance: 'integer',
    urgency: 'integer',
    priority: 'integer',
    start_date: 'date',
    due_date: 'datetime',
    billable: 'boolean',
    estimate_effort_h: 'decimal',
    monetary_cost: 'decimal',
    location: 'string',
    necessity: 'string',
    start_at: 'datetime',
    duration_minutes: 'integer',
    parent_task_id: 'string',
    budget_id: 'string',
    required_capabilities: 'json',
    recurrence: 'json',
    state_id: 'string',
    is_archived: 'boolean',
  },
  note: {
    title: 'string',
    transcript: 'string',
    summary: 'string',
    status: 'string',
    maturity: 'string',
    kind: 'string',
    is_archived: 'boolean',
    project_id: 'string',
  },
}

/** Normalize a value into the canonical comparable form for its
 * type. Identical normalization is applied on both sides of the
 * diff (snapshot + current) so an ISO date stored as ``"2026-05-26"``
 * matches a ``Date`` object pointing at the same calendar day, and a
 * Decimal serialised as ``"12.50"`` matches the ``12.5`` number that
 * the SPA carries in its editor state.
 *
 * Returns ``null`` for absent / unparseable values so missing-on-both
 * sides counts as equal.
 */
export function normalize(value: unknown, type: FieldType): unknown {
  if (value === null || value === undefined) return null
  switch (type) {
    case 'date': {
      // Strip the time-of-day; compare on the calendar day in UTC.
      // Server emits "YYYY-MM-DD"; client may carry a Date object.
      const ms = parseDateMs(value)
      if (ms === null) return String(value)
      const d = new Date(ms)
      // YYYY-MM-DD in UTC, stable.
      return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`
    }
    case 'datetime': {
      const ms = parseDateMs(value)
      return ms === null ? String(value) : ms
    }
    case 'decimal': {
      const n = parseNumber(value)
      return n === null ? String(value) : n
    }
    case 'integer': {
      const n = parseNumber(value)
      return n === null ? String(value) : Math.trunc(n)
    }
    case 'boolean':
      return Boolean(value)
    case 'string':
      return String(value)
    case 'json':
      try {
        return stableStringify(value)
      } catch {
        return String(value)
      }
  }
}

/** Equality check used by the diff to decide if a field has
 * changed. Symmetric in its arguments. */
export function equal(a: unknown, b: unknown, type: FieldType): boolean {
  return normalize(a, type) === normalize(b, type)
}

/** Human-readable rendering for the diff cell. Drives the display
 * column, never the comparison. Accepts an i18n ``t`` so booleans
 * and "missing" placeholders can be localized. */
export function display(value: unknown, type: FieldType, t: TFunction): string {
  if (value === null || value === undefined || value === '') {
    return t('common.dashEmpty', { defaultValue: '—' })
  }
  switch (type) {
    case 'date': {
      const ms = parseDateMs(value)
      if (ms === null) return String(value)
      return new Date(ms).toLocaleDateString()
    }
    case 'datetime': {
      const ms = parseDateMs(value)
      if (ms === null) return String(value)
      return new Date(ms).toLocaleString()
    }
    case 'decimal': {
      const n = parseNumber(value)
      if (n === null) return String(value)
      return n.toLocaleString(undefined, {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      })
    }
    case 'integer': {
      const n = parseNumber(value)
      if (n === null) return String(value)
      return String(Math.trunc(n))
    }
    case 'boolean':
      return value
        ? t('common.yes', { defaultValue: 'Yes' })
        : t('common.no', { defaultValue: 'No' })
    case 'string':
      return String(value)
    case 'json':
      try {
        return JSON.stringify(value)
      } catch {
        return String(value)
      }
  }
}

/** Look up a field's type for the given entity kind. Falls back to
 * ``string`` (verbatim toString) for unknown columns. */
export function fieldType(kind: 'task' | 'note', field: string): FieldType {
  return FIELD_TYPES[kind][field] ?? 'string'
}

// ────────────────────────────────────────────────────────────────────
// Internals
// ────────────────────────────────────────────────────────────────────

function pad(n: number): string {
  return n < 10 ? `0${n}` : String(n)
}

/** Parse a "date-ish" value into millis since epoch. Accepts ISO
 * strings, ``Date`` instances, and numbers (already-epoch). Returns
 * ``null`` if the value can't be parsed. */
function parseDateMs(value: unknown): number | null {
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value.getTime()
  if (typeof value === 'number') return Number.isFinite(value) ? value : null
  if (typeof value === 'string') {
    const ms = Date.parse(value)
    return Number.isNaN(ms) ? null : ms
  }
  return null
}

function parseNumber(value: unknown): number | null {
  if (typeof value === 'number') return Number.isFinite(value) ? value : null
  if (typeof value === 'string') {
    if (value.trim() === '') return null
    const n = Number(value)
    return Number.isFinite(n) ? n : null
  }
  return null
}

/** Deterministic stringify: object keys are sorted recursively so
 * ``{a:1,b:2}`` and ``{b:2,a:1}`` collapse to the same string. */
function stableStringify(value: unknown): string {
  return JSON.stringify(value, (_k, v) => {
    if (v && typeof v === 'object' && !Array.isArray(v)) {
      const o = v as Record<string, unknown>
      const sorted: Record<string, unknown> = {}
      for (const k of Object.keys(o).sort()) sorted[k] = o[k]
      return sorted
    }
    return v
  })
}
