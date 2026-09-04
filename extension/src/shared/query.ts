// The query line's grammar.
//
// It is a STRICT SUBSET of the /tasks filter language plus two sigils of
// its own, and the difference between the two surfaces is not cosmetic.
//
// On /tasks an unrecognised `key:value` degrades to free text, and that
// is right there: the predicate runs over a list the page already holds,
// so a mis-parsed atom merely fails to narrow. Here the query goes to a
// server that has ALREADY cut its answer to the top twenty, so the same
// degradation silently changes which twenty. An atom this surface cannot
// honour is therefore dropped from the request and shown as unresolved,
// never quietly reinterpreted.
//
// The tokenizer, the predicate pattern and the closed key set come from
// web/src/shared: they are the half both surfaces must agree on, and
// `@name` means TAG on both. A key this file adds that /tasks does not
// know is refused by extension/scripts/check-query-grammar.mjs, so the
// two grammars cannot quietly become two languages.

import { RE_PREDICATE, isFilterKey, tokenize } from '@shared'

/** The sigils this surface adds. Everything else must already be a key
 *  the /tasks grammar knows. Read by the drift gate, so it is data
 *  rather than a chain of comparisons. */
export const SCOPE_SIGILS = ['in', 'is'] as const

/** `is:` takes a fixed vocabulary; anything else under it is unresolved
 *  rather than free text, because `is:` is unambiguously structured. */
export const IS_VALUES = ['task', 'note', 'archived'] as const
export type IsValue = (typeof IS_VALUES)[number]

export interface ScopeRef {
  /** What the user typed after `in:`. Resolved against workspaces,
   *  clients and projects by the caller, which is the only thing that
   *  knows what exists. `*` means everything. */
  needle: string
}

export interface ParsedQuery {
  /** What is left for the ranked search. */
  text: string
  kinds: ('task' | 'note')[]
  includeArchived: boolean
  /** Present when the query overrode the persistent selection. */
  scope: ScopeRef | null
  /** `@name` references, ANDed, exactly as on /tasks. */
  tags: string[]
  /** Atoms this surface cannot honour. They are DROPPED from the request
   *  and shown, so a person can see that the query they typed is not the
   *  query that ran. */
  unresolved: string[]
}

export function parseQuery(input: string): ParsedQuery {
  const out: ParsedQuery = {
    text: '',
    kinds: ['task', 'note'],
    includeArchived: false,
    scope: null,
    tags: [],
    unresolved: [],
  }
  const free: string[] = []
  const kinds = new Set<'task' | 'note'>()

  for (const token of tokenize(input)) {
    if (token === '|') {
      // Union has no server-side expression here, so it cannot be
      // honoured and must not be silently ignored either.
      out.unresolved.push(token)
      continue
    }
    if (token.startsWith('@') && token.length > 1) {
      out.tags.push(token.slice(1))
      continue
    }
    const match = RE_PREDICATE.exec(token)
    if (!match) {
      free.push(token)
      continue
    }
    const key = (match[1] ?? '').toLowerCase()
    const value = match[2] ?? ''
    if (key === 'in') {
      out.scope = { needle: value }
      continue
    }
    if (key === 'is') {
      const lower = value.toLowerCase()
      if (lower === 'task' || lower === 'note') {
        kinds.add(lower)
        continue
      }
      if (lower === 'archived') {
        out.includeArchived = true
        continue
      }
      out.unresolved.push(token)
      continue
    }
    if (key === 'tag') {
      out.tags.push(value)
      continue
    }
    // A key the /tasks grammar knows and this surface cannot express
    // server-side. Applying it here, after the server truncated, would
    // give the same token a different meaning on the two surfaces.
    out.unresolved.push(token)
    if (!isFilterKey(key)) {
      // Not even a key over there. Same outcome, different reason, and
      // the reason is worth keeping straight: this one is a typo.
      continue
    }
  }

  if (kinds.size > 0) out.kinds = [...kinds]
  out.text = free.join(' ')
  return out
}
