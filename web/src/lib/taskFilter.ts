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
//     keys: state | due | priority | created | executor | actor
//     due:  today | tomorrow | overdue | none | +Nd | -Nd | YYYY-MM-DD
//     priority: integer or comparator (<=N, <N, >=N, >N)
//     created: same as due (date keywords / relative)
//     state: name or ! prefix
//     executor: human | llm_agent | offered  (narrow: assignee identity)
//     actor: human | bot                     (matches the card badge —
//       broader than executor: a task with NULL assignee that was
//       created via an MCP token / ai_assistant identity is "bot",
//       same way the card surfaces an AI badge.)
//   freeText: anything else, matches against title (case-insensitive)
//
// Unknown keys / malformed tokens degrade to free-text — easier than
// throwing parser errors at the user.

import type { components } from '../api/schema'

// Per-tab memory of the last /tasks URL search (``?q=…&filter=…``). The
// query and tag filter live in the URL (source of truth) so the browser
// Back button restores them; this key lets the task detail view's
// in-app "back to tasks" link return to the same filtered list whatever
// entry point opened the task. sessionStorage, so it is tab-scoped and
// ephemeral.
export const TASKS_LASTSEARCH_KEY = 'flow.tasks.lastSearch'

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

// Free-text tokens are everything that isn't a structured atom
// (``@tag``, ``state:``, ``due:``, ``priority:``, ``created:``,
// ``executor:``, ``actor:``, ``tag:``) or an OR pipe. They're the
// payload the server-side /search call uses; the structured atoms stay
// client-side so a refinement like ``state:in_progress`` doesn't need a
// roundtrip. Negated atoms (``!@done``) are preserved structurally;
// ``!freeText`` is dropped here (server can't express negative free
// text in this contract).
export function getFreeTextTokens(input: string): string[] {
  const out: string[] = []
  for (const tok of tokenize(input)) {
    if (tok === '|') continue
    const inner = tok
    if (inner.startsWith('!')) {
      // Negated structured atoms (``!@done``, ``!state:done``) are
      // handled in the predicate; ``!plain`` is too rare to round-trip.
      continue
    }
    if (inner.startsWith('@')) continue
    if (RE_PREDICATE.test(inner)) continue
    out.push(inner)
  }
  return out
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
    if (key === 'actor') return compileActor(value)
  }
  // free text → match title, description, checklist item text, or any
  // tag name. Description/checklist coverage is what lets "pane"
  // surface a "Shopping list" task whose item is "pane" without
  // having to remember the parent title. ``checklist`` is populated
  // only when the caller asked for ``include_checklist`` on /tasks
  // (TasksRoute does). Other callers see ``checklist=[]`` and the
  // .some() simply yields false — no behaviour change for them.
  const needle = token.toLowerCase()
  return (t) =>
    t.title.toLowerCase().includes(needle) ||
    (t.description ?? '').toLowerCase().includes(needle) ||
    (t.checklist ?? []).some((it) => it.text.toLowerCase().includes(needle)) ||
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

// Matches the badge cascade in TaskKanban / RecentTasks / quick-add:
// first the assignee identity, else the creator if AI, else the legacy
// executor_kind. ``executor:`` is too narrow for the /tasks toggle —
// MCP-created tasks with NULL assignee carry executor_kind=human (the
// column default) so "Bots" never matched them, even though the cards
// show an AI badge. ``actor:bot`` reproduces the badge predicate so the
// filter result matches what the user sees.
function isActorBot(t: Task): boolean {
  if (t.assignee_kind) return t.assignee_kind === 'ai_assistant'
  if (t.created_by_kind === 'ai_assistant' || t.created_by_kind === 'mcp_token') return true
  return t.executor_kind === 'llm_agent'
}

function compileActor(value: string): FilterPredicate {
  if (value === 'bot') return isActorBot
  if (value === 'human') return (t) => !isActorBot(t)
  return () => false
}

// Migration 0005: due_date is an ISO timestamp (with time-of-day).
// All ``due:`` filters are calendar-day predicates, so we compare the
// LOCAL date part — the user's "due today" means today in their tz,
// not "today UTC".
function dueLocalYmd(iso: string | null | undefined): string | null {
  if (!iso) return null
  const d = new Date(iso)
  if (!Number.isFinite(d.getTime())) return null
  return ymd(d)
}

function compileDue(value: string, ctx: FilterCtx): FilterPredicate {
  const lower = value.toLowerCase()
  if (lower === 'none' || lower === 'no')
    return (t) => t.due_date == null
  if (lower === 'today') {
    const today = ymd(ctx.now)
    return (t) => dueLocalYmd(t.due_date) === today
  }
  if (lower === 'tomorrow') {
    const d = new Date(ctx.now)
    d.setDate(d.getDate() + 1)
    const tom = ymd(d)
    return (t) => dueLocalYmd(t.due_date) === tom
  }
  if (lower === 'overdue') {
    const today = ymd(ctx.now)
    return (t) => {
      const dueYmd = dueLocalYmd(t.due_date)
      return dueYmd !== null && dueYmd < today
    }
  }
  const rel = /^([+-])(\d+)d$/.exec(lower)
  if (rel) {
    const sign = rel[1] === '-' ? -1 : 1
    const days = Number(rel[2])
    const d = new Date(ctx.now)
    d.setDate(d.getDate() + sign * days)
    const target = ymd(d)
    return (t) => dueLocalYmd(t.due_date) === target
  }
  // absolute YYYY-MM-DD
  if (/^\d{4}-\d{2}-\d{2}$/.test(value))
    return (t) => dueLocalYmd(t.due_date) === value
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
