import {
  acceptCompletion,
  autocompletion,
  type Completion,
  type CompletionContext,
  type CompletionResult,
} from '@codemirror/autocomplete'
import { Prec, type Extension } from '@codemirror/state'
import { keymap } from '@codemirror/view'
import { formatMentionHref } from '../mentions'
import { mdLink } from '../markdownInline'
import { searchCandidates, searchEntities } from '../mentionSearch'

// The two typeaheads, as CodeMirror completion sources.
//
// They match on what the user TYPES, not on what gets inserted. That sounds
// obvious and is the trap: the `@` typeahead's output is `[label](@task:id)`,
// so a source keyed on `@task:` would fire only after the completion had
// already happened, i.e. never. What the user types is `@` followed by a
// free-text title, and `[[` followed by a title or an 8-hex id prefix.
//
// Both insert exactly what the tiptap surface inserts, because a body has to
// read the same whichever surface wrote it:
//   `@`  -> `[label](@kind:uuid) `
//   `[[` -> `` `<8 hex>` `` plus a space (the ADR-0038 chip convention)
//
// The label goes through the shared escaper: a task called `Fix ] the parser`
// interpolated raw would emit a string that is not a link.

/**
 * What opens the `@` typeahead: an at-sign at a word boundary, followed by a
 * free-text query up to the caret. A space ends it, exactly as in the tiptap
 * surface -- an unterminated `@` mid-sentence must not keep matching the rest
 * of the paragraph.
 */
export const MENTION_TRIGGER = /(?:^|\s)@([^\s@]*)$/
/** What opens the `[[` typeahead: the double bracket plus a query. */
export const ENTITY_TRIGGER = /\[\[([^\]\n]*)$/

/** The captured query of a trigger, for text that is known to match. */
function queryOf(text: string, re: RegExp): string {
  return re.exec(text)?.[1] ?? ''
}

/** What a `@` completion inserts: the mention link plus a trailing space. */
export function mentionCompletion(kind: string, id: string, label: string): Completion {
  return {
    label,
    detail: kind,
    // A trailing space so typing continues outside the link, matching what
    // the tiptap command inserts.
    apply: mdLink(label, formatMentionHref(kind as never, id)) + ' ',
  }
}

export async function mentionSource(ctx: CompletionContext): Promise<CompletionResult | null> {
  const match = ctx.matchBefore(MENTION_TRIGGER)
  if (!match) return null
  const query = queryOf(match.text, MENTION_TRIGGER)
  // Without an explicit request, wait for something to search on: firing on
  // a bare `@` would query the whole workspace on every at-sign typed in
  // prose ("scrivimi @ domani").
  if (!ctx.explicit && query.length < 1) return null
  const items = await searchCandidates(query)
  if (ctx.aborted) return null
  return {
    // The replaced range starts at the `@`, not at the whitespace the regex
    // needed for its left boundary.
    from: match.to - query.length - 1,
    options: items.map((c) => mentionCompletion(c.kind, c.id, c.label)),
    // NO CLIENT-SIDE FILTER, and this is the difference between a working
    // typeahead and a silent one. CodeMirror otherwise re-filters the
    // options against the text between `from` and the caret -- which here
    // starts at the `@`, so it would try to fuzzy-match `@fi` against a task
    // called `Fix it`, find no `@` in the label, and show nothing at all.
    // The search already ranked these server-side; re-ranking them against
    // a trigger character is not an improvement.
    filter: false,
    // Re-query as the user keeps typing rather than reusing the first
    // answer: the result set is the server's, and it changes.
    validFor: undefined,
  }
}

export async function entitySource(ctx: CompletionContext): Promise<CompletionResult | null> {
  const match = ctx.matchBefore(ENTITY_TRIGGER)
  if (!match) return null
  const query = queryOf(match.text, ENTITY_TRIGGER)
  const items = await searchEntities(query)
  if (ctx.aborted) return null
  return {
    from: match.to - query.length - 2,
    options: items.map((c) => ({
      label: c.label,
      detail: c.kind,
      // The backticked 8-hex prefix (ADR-0038), which the renderer and this
      // editor both turn into a clickable chip.
      apply: '`' + c.id.replace(/-/g, '').slice(0, 8) + '` ',
    })),
    // Same reason as above: the typed text starts with `[[`.
    filter: false,
    validFor: undefined,
  }
}

/** The `@` and `[[` typeaheads. */
export function markdownCompletion(): Extension {
  return [
    autocompletion({
      override: [mentionSource, entitySource],
      // The editor is a writing surface: completions are asked for, never
      // pushed. `activateOnTyping` stays on because both sources need an
      // explicit trigger character before they return anything at all.
      activateOnTyping: true,
      closeOnBlur: true,
      icons: false,
    }),
    // Enter has to ACCEPT an open completion, and that needs the highest
    // precedence rather than the default completion keymap's: `markdown()`
    // binds Enter at HIGH precedence for list continuation, so with the
    // ordinary binding the popup opened, the arrow keys moved through it,
    // and Enter inserted a newline instead of picking anything.
    //
    // `acceptCompletion` returns false when no completion is open, so this
    // is inert the rest of the time and Enter still continues a list.
    Prec.highest(keymap.of([{ key: 'Enter', run: acceptCompletion }])),
  ]
}
