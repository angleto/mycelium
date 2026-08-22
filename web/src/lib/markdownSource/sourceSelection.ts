import { syntaxTree } from '@codemirror/language'
import type { EditorState } from '@codemirror/state'
import type { EditorView } from '@codemirror/view'

// Reading a selection as a SOURCE-domain annotation anchor.
//
// The tiptap surface captured `doc.textBetween(from, to, ' ')`: a projection
// of the document with the markup stripped. Here the selection already IS a
// span of the markdown, so the quote is `sliceDoc(from, to)` and there is no
// projection to keep in step with the server's.
//
// Two things are done to the raw range before it becomes an anchor, and both
// are about making the anchor LOCATABLE later rather than about what the user
// meant right now.

/** Inline containers whose delimiters come in pairs. Half a pair is markup
 *  with nothing to close it. */
const PAIRED = new Set(['Emphasis', 'StrongEmphasis', 'Strikethrough', 'InlineCode', 'Link'])

/** The delimiter children of a paired node. */
const MARKS = new Set(['EmphasisMark', 'StrikethroughMark', 'CodeMark', 'LinkMark'])

/** Block-level containers, for taking an anchor's context. */
const BLOCKS = new Set([
  'Paragraph',
  'ATXHeading1',
  'ATXHeading2',
  'ATXHeading3',
  'ATXHeading4',
  'ATXHeading5',
  'ATXHeading6',
  'SetextHeading1',
  'SetextHeading2',
  'Blockquote',
  'ListItem',
  'BulletList',
  'OrderedList',
  'FencedCode',
  'CodeBlock',
  'Table',
  'TableRow',
])

/** Characters of context on each side. Matches the server's `_CONTEXT`, so a
 *  freshly captured anchor and one migration 0099 converted are the same
 *  shape and the locator cannot tell them apart. */
export const CONTEXT = 24

function nodeAt(state: EditorState, pos: number, side: -1 | 1) {
  return syntaxTree(state).resolveInner(pos, side)
}

function intersects(a: { from: number; to: number }, from: number, to: number): boolean {
  return a.from < to && from < a.to
}

/**
 * Grow a range so it never covers ONE delimiter of a pair without the other.
 *
 * Selecting from the text of `**bold**` to somewhere inside the closing
 * asterisks yields the quote `bold**`, and splicing a replacement into that
 * leaves `**X` -- valid markdown that renders as literal asterisks. The
 * server names that residue and cannot fix it (an agent can send any quote),
 * but a HUMAN never has to produce it: a selection is a gesture, and the
 * gesture meant the whole run.
 *
 * Note what this deliberately does NOT do: a range covering NEITHER
 * delimiter is left alone. Selecting just `bold` inside `**bold**` is the
 * ordinary way to comment on a word, and growing it would annotate something
 * the reader did not point at.
 *
 * Iterated to a fixed point, because growing out of one run can land halfway
 * into an enclosing one.
 */
export function snapToInlineBoundaries(
  state: EditorState,
  from: number,
  to: number,
): { from: number; to: number } {
  let a = from
  let b = to
  for (let guard = 0; guard < 8; guard += 1) {
    let grew = false
    syntaxTree(state).iterate({
      from: Math.max(0, a - 1),
      to: Math.min(state.doc.length, b + 1),
      enter: (node) => {
        if (!PAIRED.has(node.name)) return
        if (a <= node.from && node.to <= b) return
        const marks: { from: number; to: number }[] = []
        for (let c = node.node.firstChild; c; c = c.nextSibling) {
          if (MARKS.has(c.name)) marks.push({ from: c.from, to: c.to })
        }
        if (marks.length < 2) return
        const openIn = intersects(marks[0], a, b)
        const closeIn = intersects(marks[marks.length - 1], a, b)
        if (openIn === closeIn) return
        a = Math.min(a, node.from)
        b = Math.max(b, node.to)
        grew = true
      },
    })
    if (!grew) break
  }
  return { from: Math.min(a, b), to: Math.max(a, b) }
}

/** Shrink a range inward past whitespace, so the quote and the highlighted
 *  span are the same characters and neither starts nor ends on a space (an
 *  edge space makes an anchor needlessly fragile). */
function trimRange(state: EditorState, from: number, to: number): { from: number; to: number } {
  const text = state.sliceDoc(from, to)
  const lead = /^\s*/.exec(text)?.[0].length ?? 0
  const trail = /\s*$/.exec(text)?.[0].length ?? 0
  if (lead + trail >= text.length) return { from, to: from }
  return { from: from + lead, to: to - trail }
}

/** The enclosing block of a position, for taking context from. Falls back to
 *  the line, which is what an unparsed or top-level position deserves. */
function enclosingBlock(state: EditorState, pos: number): { from: number; to: number } {
  for (let n = nodeAt(state, pos, -1); n; n = n.parent as never) {
    if (BLOCKS.has(n.name)) return { from: n.from, to: n.to }
    if (!n.parent) break
  }
  const line = state.doc.lineAt(pos)
  return { from: line.from, to: line.to }
}

/** An annotation anchor read off the document. No view, no layout: this is
 *  the part that has to be right, and it is a pure function of the state. */
export type SourceAnchor = {
  from: number
  to: number
  /** The markdown source between from and to. */
  text: string
  prefix: string
  suffix: string
}

/** Where to put a popover for an anchor. Separate from the anchor because it
 *  is the only part that needs LAYOUT, which means it is the only part that
 *  can fail for reasons having nothing to do with the document (a detached
 *  view, a headless test). An anchor without coordinates is still a valid
 *  anchor; a popover without them just has nowhere to go. */
export type SelectionCoords = { left: number; top: number; bottom: number }

export type SourceSelection = SourceAnchor & { coords: SelectionCoords | null }

/**
 * The current selection as an anchor, or null when there is nothing to
 * annotate.
 *
 * The two ends take their context INDEPENDENTLY, each from its own enclosing
 * block. Using one block for both inverts the bounds the moment a selection
 * spans two of them -- which is the ordinary way to quote a pair of
 * paragraphs, not an edge case.
 */
export function readSourceAnchor(state: EditorState): SourceAnchor | null {
  const main = state.selection.main
  if (main.empty || main.to <= main.from) return null
  const trimmed = trimRange(state, main.from, main.to)
  if (trimmed.to <= trimmed.from) return null
  const { from, to } = snapToInlineBoundaries(state, trimmed.from, trimmed.to)
  const text = state.sliceDoc(from, to)
  if (!text.trim()) return null

  const startBlock = enclosingBlock(state, from)
  const endBlock = enclosingBlock(state, to)
  return {
    from,
    to,
    text,
    prefix: state.sliceDoc(Math.max(startBlock.from, from - CONTEXT), from),
    suffix: state.sliceDoc(to, Math.min(endBlock.to, to + CONTEXT)),
  }
}

/** Viewport coordinates spanning a range, or null when the view cannot
 *  measure (which is not a reason to discard the anchor). */
export function selectionCoords(
  view: EditorView,
  from: number,
  to: number,
): SelectionCoords | null {
  try {
    const a = view.coordsAtPos(from)
    const b = view.coordsAtPos(to)
    if (!a || !b) return null
    return {
      left: (a.left + b.left) / 2,
      top: Math.min(a.top, b.top),
      bottom: Math.max(a.bottom, b.bottom),
    }
  } catch {
    return null
  }
}

/** The anchor plus where to draw a popover for it. */
export function readSourceSelection(view: EditorView): SourceSelection | null {
  const a = readSourceAnchor(view.state)
  if (!a) return null
  return { ...a, coords: selectionCoords(view, a.from, a.to) }
}
