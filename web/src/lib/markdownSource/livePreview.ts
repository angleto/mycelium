import { syntaxTree } from '@codemirror/language'
import type { EditorState, Extension, Range } from '@codemirror/state'
import {
  Decoration,
  type DecorationSet,
  EditorView,
  ViewPlugin,
  type ViewUpdate,
} from '@codemirror/view'
import { overlaps, revealedRanges } from './reveal'
import { ImageWidget, parseImageEmbed } from './widgets'
import type { ImageUploadParent } from '../imageUpload'

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
 * A setext underline never reaches here: it is folded into its heading by
 * the block layer, which can replace a range containing a line break because
 * it is a StateField.
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

function buildDecorations(
  view: EditorView,
  getParent: () => ImageUploadParent | undefined,
): DecorationSet {
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
          // A setext heading's underline is FOLDED AWAY by the block layer
          // (blockPreview.ts), which replaces the newline before it. Styling
          // that second line, or hiding its `====` here, would put a
          // decoration inside a replaced range and CodeMirror would refuse
          // the whole set. Only the heading's own line gets the class.
          const lastLine = node.name.startsWith('Setext') ? node.from : node.to
          decorateLines(out, state, node.from, lastLine, cls)
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
        if (node.name === 'Image') {
          // The whole embed becomes the picture. Descending would let the
          // hide pass below put decorations INSIDE a replaced range, which
          // CodeMirror rejects for the entire set, so this returns false
          // either way -- revealed (show the source) or not.
          if (overlaps(revealed, node.from, node.to)) return false
          const source = state.sliceDoc(node.from, node.to)
          const embed = parseImageEmbed(source)
          if (!embed) return false
          out.push(
            Decoration.replace({
              widget: new ImageWidget({ source, ...embed, getParent }),
            }).range(node.from, node.to),
          )
          return false
        }
        if (node.from === node.to) return
        const parent = node.node.parent?.name
        // Same reason: the `====` line is the block layer's to remove.
        if (node.name === 'HeaderMark' && parent?.startsWith('SetextHeading')) return
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

function makeLivePreviewPlugin(getParent: () => ImageUploadParent | undefined) {
  return ViewPlugin.fromClass(
  class {
    decorations: DecorationSet

    constructor(view: EditorView) {
      this.decorations = buildDecorations(view, getParent)
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
        this.decorations = buildDecorations(u.view, getParent)
      }
    }
  },
  { decorations: (v) => v.decorations },
  )
}

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
 *  dispatches a document change.
 *
 *  ``getParent`` is read live, per widget build, rather than captured: a
 *  brand-new note has no id when its editor is constructed, and a bare
 *  filename embed can only be resolved against a parent that exists. */
export function livePreview(opts: {
  getParent?: () => ImageUploadParent | undefined
}): Extension {
  const getParent = opts.getParent ?? (() => undefined)
  return [makeLivePreviewPlugin(getParent), linkLabelPlugin]
}
