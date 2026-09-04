// Resolving an 8-hex entity code, as a contract rather than a client.
//
// Notes and roadmap text refer to tasks and notes by a UUID prefix in
// backticks (`91cf6aaa`). The markdown renderer turns those into
// clickable chips, the short URLs /t/:prefix and /n/:prefix resolve
// them, the command palette opens them, and the extension's panel does
// all three from outside the app. Every one of those callers builds the
// same request and must agree on the same two things: what counts as a
// code-shaped input, and what perimeter the question is asked in.
//
// TWO INTENTS, one endpoint (task d12f6217). Resolving an id -- a chip,
// a short URL, the panel's code branch -- asks "what entity IS this?",
// and the answer must not depend on whether the entity sits on the
// archive shelf: those callers pass RESOLVE_ID and render the state the
// match reports. Offering a LIST of candidates (the mention picker) asks
// "what may I link to from here?", and keeps the endpoint's default,
// which is the perimeter GET /notes and GET /tasks show. The flag is
// therefore never a detail of the fetch; it is which of the two
// questions the caller is asking, which is why it is spelled out at
// every call site instead of defaulted.
//
// The cache key includes the perimeter for the same reason: the same
// prefix asked as two different questions has two different answers,
// and one cache entry for both would let the narrower question serve
// the wider one's result.
//
// Pure by contract: this directory is compiled into more than one
// package and must import nothing. The transport, the cache and the
// tenant-change invalidation belong to each client.

export interface LookupMatch {
  kind: 'task' | 'note'
  id: string
  title: string | null
  state_name: string | null
  is_terminal: boolean | null
  is_archived: boolean
  is_deleted: boolean
  route_url: string
}

export interface LookupOut {
  prefix: string
  matches: LookupMatch[]
}

export interface LookupOpts {
  kinds?: readonly ('task' | 'note')[]
  /** Resolve entities on the archive shelf too. They come back with
   *  ``is_archived: true``, so the caller can (and should) show it. */
  includeArchived?: boolean
}

/** The perimeter for "what entity is this id?": the archive shelf must
 *  not hide an entity from its own identifier. Spread it
 *  (``{...RESOLVE_ID, kinds: [...]}``) so every resolution call site
 *  says which question it is asking instead of leaning on a default. */
export const RESOLVE_ID: LookupOpts = { includeArchived: true }

// A bare hex run, or a hyphenated one up to a full UUID. Anchored at
// both ends on a hex digit so a trailing hyphen from a half-typed UUID
// does not read as a code and fire a lookup for something nobody asked
// for.
const HEX_PREFIX_RE = /^[0-9a-f][0-9a-f-]{2,34}[0-9a-f]$/i

const FULL_UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

export function isPrefixCandidate(raw: string): boolean {
  return HEX_PREFIX_RE.test(raw.trim().toLowerCase())
}

export function isFullUuid(raw: string): boolean {
  return FULL_UUID_RE.test(raw.trim())
}

/** ``${prefix}|${kinds}|${perimeter}``. Kinds are sorted so that a
 *  caller writing ['note','task'] and one writing ['task','note'] share
 *  an entry instead of paying twice for one answer. */
export function lookupCacheKey(prefix: string, opts: LookupOpts = {}): string {
  const kinds = opts.kinds
  const k = kinds && kinds.length ? [...kinds].sort().join(',') : 'task,note'
  return `${prefix.trim().toLowerCase()}|${k}|${opts.includeArchived ? 'a' : ''}`
}

/** The request path, relative to the API root. Built here so the two
 *  clients cannot disagree about the parameter names, and encoded
 *  because a prefix is user input reaching a URL. */
export function lookupPath(prefix: string, opts: LookupOpts = {}): string {
  const qs = new URLSearchParams()
  if (opts.kinds && opts.kinds.length) qs.set('kinds', [...opts.kinds].join(','))
  if (opts.includeArchived) qs.set('include_archived', 'true')
  const q = qs.toString()
  return `/lookup/${encodeURIComponent(prefix.trim().toLowerCase())}${q ? `?${q}` : ''}`
}
