import type { EditorState } from '@codemirror/state'

// The one rule both preview layers obey: markup shows on the lines the
// selection touches, and recedes everywhere else. Shared so the inline layer
// and the block layer can never disagree about which lines are revealed --
// if they did, one would hide a construct's delimiters while the other left
// its source visible, and the result would be neither the source nor the
// rendering.

export type LineRange = { from: number; to: number }

/** Whole lines touched by any selection range. */
export function revealedRanges(state: EditorState): LineRange[] {
  return state.selection.ranges.map((r) => ({
    from: state.doc.lineAt(r.from).from,
    to: state.doc.lineAt(r.to).to,
  }))
}

export function overlaps(ranges: LineRange[], from: number, to: number): boolean {
  return ranges.some((r) => from <= r.to && to >= r.from)
}
