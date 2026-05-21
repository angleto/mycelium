// Todoist-inspired tasks filter DSL. Parses a short query string into
// a predicate the route applies to its task list. Used by /tasks's
// search box: free-text still matches the title, but the input now
// also understands tag references, due-date keywords, state filters,
// priority comparisons, and union (``|``) / negation (``!``).
//
// Grammar (informal):
//   query   := orclause (' ' orclause)*           (implicit AND)
//   orclause := atom ('|' atom)+ | atom
//   atom    := '!' atom | tagref | predicate | freeText
//   tagref  := '@' name | 'tag:' name
//   predicate := key ':' value
//     keys: state | due | priority | created | executor
//     due:  today | tomorrow | overdue | none | +Nd | -Nd | YYYY-MM-DD
//     priority: integer or comparator (<=N, <N, >=N, >N)
//     created: same as due (date keywords / relative)
//     state: name or ! prefix
//     executor: human | llm_agent | offered
//   freeText: anything else, matches against title (case-insensitive)
//
// Unknown keys / malformed tokens degrade to free-text — easier than
// throwing parser errors at the user.

import type { components } from '../api/schema'

type Task = components['schemas']['TaskOut']

export type FilterCtx = {
  tagsById: Map<string, { name: string; kind: string }>
  statesById: Map<string, { name: string; is_terminal: boolean }>
  now: Date
}

export type FilterPredicate = (t: Task) => boolean

const RE_PREDICATE = /^([a-z_]+):(.+)$/i

export function parseFilter(input: string, ctx: FilterCtx): FilterPredicate {
  const tokens = tokenize(input)
  if (tokens.length === 0) return () => true
  const ands: FilterPredicate[] = []
  // Implicit AND between top-level groups; OR pieces joined by '|'
  // are picked up here as a single multi-token cluster.
  let i = 0
  while (i < tokens.length) {
    const ors: FilterPredicate[] = [compileAtom(tokens[i], ctx)]
    while (i + 1 < tokens.length && tokens[i + 1] === '|' && i + 2 < tokens.length) {
      ors.push(compileAtom(tokens[i + 2], ctx))
      i += 2
    }
    if (ors.length === 1) ands.push(ors[0])
    else ands.push((t) => ors.some((f) => f(t)))
    i += 1
  }
  return (t) => ands.every((f) => f(t))
}

function tokenize(input: string): string[] {
  // Whitespace-separated, but ``|`` keeps as its own token even when
  // adjacent to atoms (``a|b`` → ``a | b``).
  const expanded = input.replace(/\s*\|\s*/g, ' | ')
  return expanded
    .split(/\s+/)
    .map((s) => s.trim())
    .filter(Boolean)
}

function compileAtom(token: string, ctx: FilterCtx): FilterPredicate {
  if (token.startsWith('!') && token.length > 1) {
    const inner = compileAtom(token.slice(1), ctx)
    return (t) => !inner(t)
  }
  if (token.startsWith('@') && token.length > 1) {
    return compileTagRef(token.slice(1))
  }
  const m = RE_PREDICATE.exec(token)
  if (m) {
    const key = m[1].toLowerCase()
    const value = m[2]
    if (key === 'tag') return compileTagRef(value)
    if (key === 'state') return compileState(value, ctx)
    if (key === 'due') return compileDue(value, ctx)
    if (key === 'priority') return comparator((t) => t.priority ?? Infinity, value)
    if (key === 'created') return compileCreated(value, ctx)
    if (key === 'executor') return compileExecutor(value)
  }
  // free text → match title or any tag name
  const needle = token.toLowerCase()
  return (t) =>
    t.title.toLowerCase().includes(needle) ||
    (t.tags ?? []).some((g) => g.name.toLowerCase().includes(needle))
}

function compileTagRef(needle: string): FilterPredicate {
  const lower = needle.toLowerCase()
  return (t) => (t.tags ?? []).some((g) => g.name.toLowerCase() === lower)
}

function compileState(value: string, ctx: FilterCtx): FilterPredicate {
  const neg = value.startsWith('!')
  const name = (neg ? value.slice(1) : value).toLowerCase()
  return (t) => {
    const s = ctx.statesById.get(t.state_id)
    const match = s?.name.toLowerCase() === name
    return neg ? !match : match
  }
}

function compileExecutor(value: string): FilterPredicate {
  if (value === 'offered') return (t) => t.offered === true
  return (t) => t.executor_kind === value
}

function compileDue(value: string, ctx: FilterCtx): FilterPredicate {
  const lower = value.toLowerCase()
  if (lower === 'none' || lower === 'no')
    return (t) => t.due_date == null
  if (lower === 'today') {
    const today = ymd(ctx.now)
    return (t) => t.due_date === today
  }
  if (lower === 'tomorrow') {
    const d = new Date(ctx.now)
    d.setDate(d.getDate() + 1)
    const tom = ymd(d)
    return (t) => t.due_date === tom
  }
  if (lower === 'overdue') {
    const today = ymd(ctx.now)
    return (t) => !!t.due_date && t.due_date < today
  }
  const rel = /^([+-])(\d+)d$/.exec(lower)
  if (rel) {
    const sign = rel[1] === '-' ? -1 : 1
    const days = Number(rel[2])
    const d = new Date(ctx.now)
    d.setDate(d.getDate() + sign * days)
    const target = ymd(d)
    return (t) => t.due_date === target
  }
  // absolute YYYY-MM-DD
  if (/^\d{4}-\d{2}-\d{2}$/.test(value))
    return (t) => t.due_date === value
  return () => false
}

function compileCreated(value: string, ctx: FilterCtx): FilterPredicate {
  // ``created:before:-Nd``, ``created:after:YYYY-MM-DD`` etc. Split on ':'.
  const parts = value.split(':')
  if (parts.length !== 2) return () => false
  const [op, raw] = parts
  const target = resolveDate(raw, ctx.now)
  if (!target) return () => false
  // ``created_at`` is not on TaskOut today (deleted_at + version are);
  // until the schema exposes it, ``created`` is best-effort no-op.
  // Hook is in place so adding the column lights it up.
  void op
  void target
  return () => true
}

function resolveDate(value: string, now: Date): string | null {
  const lower = value.toLowerCase()
  if (lower === 'today') return ymd(now)
  const rel = /^([+-])(\d+)d$/.exec(lower)
  if (rel) {
    const sign = rel[1] === '-' ? -1 : 1
    const d = new Date(now)
    d.setDate(d.getDate() + sign * Number(rel[2]))
    return ymd(d)
  }
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return value
  return null
}

function comparator(get: (t: Task) => number, expr: string): FilterPredicate {
  const m = /^(<=|>=|<|>|=)?(\d+)$/.exec(expr)
  if (!m) return () => false
  const op = m[1] || '='
  const n = Number(m[2])
  return (t) => {
    const v = get(t)
    switch (op) {
      case '<':
        return v < n
      case '<=':
        return v <= n
      case '>':
        return v > n
      case '>=':
        return v >= n
      default:
        return v === n
    }
  }
}

function ymd(d: Date): string {
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}
