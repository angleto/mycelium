import type { EditorState } from '@codemirror/state'

// The one rule both preview layers obey: the CONSTRUCT the selection touches
// shows its source, and everything else stays rendered. Shared so the inline
// layer and the block layer can never disagree about what is revealed -- if
// they did, one would hide a construct's delimiters while the other left its
// source visible, and the result would be neither the source nor the
// rendering.
//
// The scope used to be the whole LINE, and that is what made the surface read
// as neither one thing nor the other: clicking anywhere in a paragraph turned
// the entire paragraph back into markup, so every `**`, every `[`, every
// `](url)` came back at once and the text jumped. Per construct, a caret in a
// bold word shows that word's `**` and leaves the rest of the line rendered.
//
// The ranges are returned unwidened; `overlaps` is inclusive at both ends, so
// a caret sitting at either edge of a construct counts as touching it. That
// matters: it is what makes the delimiters visible at exactly the moments
// their position is ambiguous (typing at a boundary, backspacing into one).

export type LineRange = { from: number; to: number }

/** The selection's own ranges. */
export function revealedRanges(state: EditorState): LineRange[] {
  return state.selection.ranges.map((r) => ({ from: r.from, to: r.to }))
}

export function overlaps(ranges: LineRange[], from: number, to: number): boolean {
  return ranges.some((r) => from <= r.to && to >= r.from)
}
