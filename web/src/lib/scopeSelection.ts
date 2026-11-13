// A scope picker's state, as a map from catalogue key to ticked, and the two
// conversions every user of the picker needs. Pure, so the assistant form and
// the OAuth consent page convert the same way (components/ScopePicker.tsx).

import type { Scope } from './aiAssistants'

export type ScopeSelection = Record<string, boolean>

/** The selection as the ordered list of keys the server takes. */
export function selectedKeys(catalog: readonly Scope[], perms: ScopeSelection): string[] {
  return catalog.filter((p) => perms[p.key]).map((p) => p.key)
}

/** A selection with exactly ``keys`` ticked. */
export function selectionOf(catalog: readonly Scope[], keys: readonly string[]): ScopeSelection {
  const on = new Set(keys)
  return Object.fromEntries(catalog.map((e) => [e.key, on.has(e.key)]))
}
