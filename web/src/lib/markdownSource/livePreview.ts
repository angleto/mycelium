import { syntaxTree } from '@codemirror/language'
import type { EditorState, Extension, Range } from '@codemirror/state'
import {
  Decoration,
  type DecorationSet,
  EditorView,
  ViewPlugin,
  type ViewUpdate,
} from '@codemirror/view'

// Live preview: markdown that LOOKS rendered while the document stays the
// markdown source.
//
// This is a decoration layer and nothing else. It never dispatches a change,
// so `state.sliceDoc()` is the same string with the layer on as with it off
// -- which is the whole reason the editor moved onto this substrate, and the
// property the tests assert first. A decoration that needed to rewrite the
// document to display it would have reinvented the serializer this replaces.
//
// The rule for what is hidden is deliberately the simplest one that is
// predictable: markup recedes everywhere except on the lines the selection
// touches. Put the caret in a heading and the `##` comes back, so you are
// always editing real source, never a rendering of it. Obsidian and Typora
// both settled on this shape; anything cleverer (hide per construct, reveal
// per construct) makes the caret's meaning depend on where inside a
// construct it sits, and that is where those editors get bug reports.

/** The document ranges whose markup stays visible: whole lines touched by
 *  any selection range. Cheap, and there is normally exactly one. */
function revealedRanges(state: EditorState): { from: number; to: number }[] {
  return state.selection.ranges.map((r) => ({
    from: state.doc.lineAt(r.from).from,
    to: state.doc.lineAt(r.to).to,
  }))
}

function overlaps(
  ranges: { from: number; to: number }[],
  from: number,
  to: number,
): boolean {
  return ranges.some((r) => from <= r.to && to >= r.from)
}

// Inline delimiters that carry no information once the thing they delimit is
// styled. `CodeMark` and `URL` are NOT in this list unconditionally: a
// `CodeMark` also marks a fenced-code fence, and a `URL` also stands alone
// inside an autolink, and hiding either would delete something the reader
// needs. They are handled by parent below.
const ALWAYS_HIDDEN = new Set(['HeaderMark', 'EmphasisMark', 'StrikethroughMark', 'QuoteMark'])

const HEADING_LINE: Record<string, string> = {
  ATXHeading1: 'cm-md-h1',
  ATXHeading2: 'cm-md-h2',
  ATXHeading3: 'cm-md-h3',
  ATXHeading4: 'cm-md-h4',
  ATXHeading5: 'cm-md-h5',
  ATXHeading6: 'cm-md-h6',
  SetextHeading1: 'cm-md-h1',
  SetextHeading2: 'cm-md-h2',
}

const hide = Decoration.replace({})

/**
 * The range to actually hide for a delimiter node.
 *
 * A BLOCK delimiter (`##`, `>`) is separated from its content by a space
 * that belongs to the markup, not to the text: hiding the mark alone renders
 * `## Titolo` as ` Titolo`, with a leading space on every heading and every
 * quoted line. So that separator goes too.
 *
 * An INLINE delimiter (`**`, `` ` ``, `[`) hugs its content, and the space
 * next to it is the author's. Eating it turns `un **grassetto** qui` into
 * `un grassettoqui`, which is why the rule is per-kind rather than global.
 *
 * A setext underline is a known imperfection, left in on purpose. Its mark
 * IS the whole `=====` line, so hiding it leaves an empty line under the
 * heading. Folding the two lines together means replacing a range that
 * CONTAINS a line break, and CodeMirror refuses that from a ViewPlugin
 * ("Decorations that replace line breaks may not be specified via plugins")
 * because it changes the line structure the view measures heights from. The
 * fix is a StateField, which is what the block-level layer (fences, `$$`
 * blocks, tables) needs anyway; setext folding moves there with it rather
 * than growing a second mechanism here.
 */
function hiddenRange(
  state: EditorState,
  node: { name: string; from: number; to: number; parent?: string },
): { from: number; to: number } {
  const line = state.doc.lineAt(node.from)
  const isSpace = (at: number) => /[ \t]/.test(state.doc.sliceString(at, at + 1))

  let { from, to } = node
  if (node.name !== 'HeaderMark' && node.name !== 'QuoteMark') return { from, to }
  while (to < line.to && isSpace(to)) to += 1
  // A closing ATX sequence (`## Titolo ##`) sits at the end of the line with
  // its separator BEFORE it; without this the heading keeps a trailing space.
  if (to >= line.to) {
    while (from > line.from && isSpace(from - 1)) from -= 1
  }
  return { from, to }
}
const lineDeco = (cls: string) => Decoration.line({ class: cls })
const markDeco = (cls: string) => Decoration.mark({ class: cls })

/** Add one line decoration per line the node spans. */
function decorateLines(
  out: Range<Decoration>[],
  state: EditorState,
  from: number,
  to: number,
  cls: string,
): void {
  const first = state.doc.lineAt(from).number
  const last = state.doc.lineAt(to).number
  for (let n = first; n <= last; n += 1) {
    out.push(lineDeco(cls).range(state.doc.line(n).from))
  }
}

function buildDecorations(view: EditorView): DecorationSet {
  const { state } = view
  const revealed = revealedRanges(state)
  const out: Range<Decoration>[] = []
  // Only over the VISIBLE ranges: the syntax tree is time-sliced and is only
  // guaranteed to exist there, so asking outside would silently produce a
  // half-decorated document that changes as parsing catches up.
  for (const { from, to } of view.visibleRanges) {
    syntaxTree(state).iterate({
      from,
      to,
      enter: (node) => {
        const cls = HEADING_LINE[node.name]
        if (cls) {
          decorateLines(out, state, node.from, node.to, cls)
          return
        }
        if (node.name === 'Blockquote') {
          decorateLines(out, state, node.from, node.to, 'cm-md-quote')
          return
        }
        if (node.name === 'FencedCode' || node.name === 'CodeBlock') {
          decorateLines(out, state, node.from, node.to, 'cm-md-code')
          return
        }
        if (node.name === 'HorizontalRule') {
          decorateLines(out, state, node.from, node.to, 'cm-md-hr')
          return
        }
        if (node.from === node.to) return
        const parent = node.node.parent?.name
        const hidden =
          ALWAYS_HIDDEN.has(node.name) ||
          // The backticks of an inline code span, but not a fence.
          (node.name === 'CodeMark' && parent === 'InlineCode') ||
          // `[`, `]`, `(`, `)` and the destination of an inline link, so the
          // label reads as the link. An Image keeps its source visible until
          // there is a widget to put in its place, and an Autolink is all
          // URL -- hiding it would leave an empty line.
          ((node.name === 'LinkMark' || node.name === 'URL' || node.name === 'LinkTitle') &&
            parent === 'Link')
        if (!hidden) return
        if (overlaps(revealed, node.from, node.to)) return
        const r = hiddenRange(state, {
          name: node.name,
          from: node.from,
          to: node.to,
          parent,
        })
        if (r.to > r.from) out.push(hide.range(r.from, r.to))
      },
    })
  }
  return Decoration.set(out, true)
}

/** Style the label of an inline link, which is what is left once the
 *  brackets and the destination are hidden. Separate from the hiding pass
 *  because it applies whether or not the line is revealed. */
function linkLabels(view: EditorView): DecorationSet {
  const out: Range<Decoration>[] = []
  for (const { from, to } of view.visibleRanges) {
    syntaxTree(view.state).iterate({
      from,
      to,
      enter: (node) => {
        if (node.name !== 'Link') return
        const marks = []
        for (let c = node.node.firstChild; c; c = c.nextSibling) {
          if (c.name === 'LinkMark') marks.push(c)
        }
        if (marks.length < 2) return
        const labelFrom = marks[0].to
        const labelTo = marks[1].from
        if (labelTo > labelFrom) out.push(markDeco('cm-md-linklabel').range(labelFrom, labelTo))
      },
    })
  }
  return Decoration.set(out, true)
}

const livePreviewPlugin = ViewPlugin.fromClass(
  class {
    decorations: DecorationSet

    constructor(view: EditorView) {
      this.decorations = buildDecorations(view)
    }

    update(u: ViewUpdate) {
      // NEVER re-derive during an IME composition. Recomputing here changes
      // the DOM under the input method and drops the accent being composed
      // (the failure `e2e/accents.spec.ts` exists for). Mapping the existing
      // set through the changes keeps every position valid until the
      // composition ends, at which point a normal update rebuilds.
      if (u.view.composing) {
        if (u.docChanged) this.decorations = this.decorations.map(u.changes)
        return
      }
      if (
        u.docChanged ||
        u.viewportChanged ||
        u.selectionSet ||
        syntaxTree(u.startState) !== syntaxTree(u.state)
      ) {
        this.decorations = buildDecorations(u.view)
      }
    }
  },
  { decorations: (v) => v.decorations },
)

const linkLabelPlugin = ViewPlugin.fromClass(
  class {
    decorations: DecorationSet

    constructor(view: EditorView) {
      this.decorations = linkLabels(view)
    }

    update(u: ViewUpdate) {
      if (u.view.composing) {
        if (u.docChanged) this.decorations = this.decorations.map(u.changes)
        return
      }
      if (u.docChanged || u.viewportChanged || syntaxTree(u.startState) !== syntaxTree(u.state)) {
        this.decorations = linkLabels(u.view)
      }
    }
  },
  { decorations: (v) => v.decorations },
)

/** The live-preview layer. Presentational: it adds decorations and never
 *  dispatches a document change. */
export function livePreview(): Extension {
  return [livePreviewPlugin, linkLabelPlugin]
}
