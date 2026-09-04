// The query grammar, as a shape rather than as an implementation.
//
// Two browser surfaces parse the same short query language: the /tasks
// search box (web/src/lib/taskFilter.ts, a predicate over an already
// loaded TaskOut[]) and the extension's panel (which turns the same
// tokens into request parameters). The two do very different things with
// a token and must never disagree about what a token IS -- a `@name`
// that means "tag" on one surface and "assignee" on the other is the
// kind of divergence nobody notices until someone's saved query returns
// the wrong rows.
//
// So the tokenizer, the predicate pattern and the closed set of keys
// live here, once, and each surface supplies its own meaning for a key
// it can honour. A surface that CANNOT honour a key must say so, not
// reinterpret the token: on /tasks an unknown key degrades to free text
// (harmless, the predicate simply matches the title), but on a surface
// that sends the query to a server which has already truncated to a
// top-N, the same degradation silently changes the result set.
//
// Pure by contract: this directory is compiled into more than one
// package and must import nothing. See ../shared/index.ts.

/** A structured atom: ``key:value``. Anything that does not match is
 *  free text, a tag reference (``@name``) or a negation (``!atom``). */
export const RE_PREDICATE = /^([a-z_]+):(.+)$/i

/** The closed set of structured keys. A surface implements a subset and
 *  declares the rest unsupported; nothing may invent an eighth key
 *  without adding it here, which is what keeps the two grammars one
 *  grammar. */
export const FILTER_KEYS = [
  'tag',
  'state',
  'due',
  'priority',
  'created',
  'executor',
  'actor',
] as const

export type FilterKey = (typeof FILTER_KEYS)[number]

const FILTER_KEY_SET: ReadonlySet<string> = new Set<string>(FILTER_KEYS)

export function isFilterKey(key: string): key is FilterKey {
  return FILTER_KEY_SET.has(key)
}

/** The ``due:`` vocabulary, shared so a date shorthand offered by one
 *  surface's completer is one the other surface can actually parse. */
export const DUE_KEYWORDS = ['today', 'tomorrow', 'overdue', 'none'] as const
export type DueKeyword = (typeof DUE_KEYWORDS)[number]

/** ``+7d`` / ``-3d``: a day offset from now, in the reader's own
 *  timezone. Every ``due:`` filter is a calendar-day predicate, so the
 *  comparison is on the LOCAL date part: "due today" means today where
 *  the person is, not today in UTC. */
export const RE_DAY_OFFSET = /^([+-])(\d+)d$/

/** An absolute calendar day. */
export const RE_YMD = /^\d{4}-\d{2}-\d{2}$/

/** Whitespace-separated, but ``|`` stays its own token even when it is
 *  adjacent to atoms (``a|b`` becomes ``a | b``), so a union does not
 *  have to be typed with spaces to be read as one. */
export function tokenize(input: string): string[] {
  return input
    .replace(/\s*\|\s*/g, ' | ')
    .split(/\s+/)
    .map((s) => s.trim())
    .filter(Boolean)
}
