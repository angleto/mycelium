import { syntaxTree } from '@codemirror/language'
import type { EditorState, Extension, Range } from '@codemirror/state'
import {
  Decoration,
  type DecorationSet,
  EditorView,
  ViewPlugin,
  type ViewUpdate,
} from '@codemirror/view'
import type { SyntaxNodeRef } from '@lezer/common'
import { overlaps, revealedRanges } from './reveal'
import { isInlineLink } from './commands'
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
// The rule for what is hidden: markup recedes except on the CONSTRUCT the
// selection touches. Put the caret in a bold word and its `**` come back,
// while the rest of the paragraph stays rendered. You are therefore always
// editing real source, never a rendering of it, without the whole line
// flipping to markup around you.
//
// The scope is per construct rather than per line because per line is what
// made this read as neither a rendered view nor a source view, and it is per
// construct rather than nothing at all because hiding unconditionally was
// measured and is worse: `x **bold*` genuinely parses as italic for one
// keystroke while `**bold**` is being typed, so the text would shrink and
// shift mid-word; and a Backspace next to an invisible delimiter deletes half
// a pair, which makes characters APPEAR. Both are fixed by the delimiters
// being visible at exactly the moment the caret is at them, which is what
// this rule says.

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
/**
 * The construct a delimiter belongs to, for the reveal test.
 *
 * A BLOCK prefix (`##`, `>`) belongs to its LINE: it is one short run at the
 * start, revealing it does not reflow the prose, and hiding it while the
 * caret is on the line makes Backspace at the first character produce
 * `##Titolo` -- which is not a heading, so two characters appear.
 *
 * An INLINE delimiter belongs to the node it delimits, so one caret never
 * reveals one half of a pair.
 */
function revealOwner(state: EditorState, node: SyntaxNodeRef): { from: number; to: number } {
  if (node.name === 'HeaderMark' || node.name === 'QuoteMark') {
    const line = state.doc.lineAt(node.from)
    return { from: line.from, to: line.to }
  }
  let owner = node.node.parent
  if (!owner) return { from: node.from, to: node.to }
  // Climb while the enclosing construct's delimiter is CONTIGUOUS with this
  // one. `***molto***` is an Emphasis wrapping a StrongEmphasis, and the two
  // spell one run of three asterisks: revealing the outer alone put a single
  // `*` on each side, which is markup for neither construct. Contiguity is
  // the test rather than mere nesting, so `**x [a](b) y**` still reveals the
  // link on its own -- there is text between the two delimiter runs, and the
  // whole point of this rule is that a caret does not turn its surroundings
  // back into source.
  for (let up = owner.parent; up; up = up.parent) {
    const marks: { from: number; to: number }[] = []
    for (let c = up.firstChild; c; c = c.nextSibling) {
      if (c.name.endsWith('Mark')) marks.push({ from: c.from, to: c.to })
    }
    if (!marks.length) break
    const opensHere = marks[0].to === owner.from
    const closesHere = marks[marks.length - 1].from === owner.to
    if (!opensHere && !closesHere) break
    owner = up
  }
  return { from: owner.from, to: owner.to }
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
          // `[`, `]`, `(`, `)` and the destination of an INLINE link, so the
          // label reads as the link. Not a reference link, a footnote or a
          // bare bracket run: lezer makes a `Link` node out of all of those,
          // and hiding their brackets drew `array[0]` as `array0` and
          // `nota[^1]` as `nota^1`, neither of which is what the reader
          // prints. An Image keeps its source visible until there is a widget
          // to put in its place, and an Autolink is all URL -- hiding it
          // would leave an empty line.
          ((node.name === 'LinkMark' || node.name === 'URL' || node.name === 'LinkTitle') &&
            parent === 'Link' &&
            !!node.node.parent &&
            isInlineLink(state, node.node.parent))
        if (!hidden) return
        // Against the OWNER, not the delimiter: a caret at one end of a bold
        // run must bring back both `**`, never one.
        const owner = revealOwner(state, node)
        if (overlaps(revealed, owner.from, owner.to)) return
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

/**
 * The faces that apply whether or not the construct is revealed: an inline
 * link's label, which is what is left once the brackets and the destination
 * are hidden, and an inline code span's content, which has to keep a
 * monospace face under the rendered view's proportional body.
 *
 * Separate from the hiding pass for exactly that reason -- these do not
 * depend on the reveal -- and marks rather than replacements, so they nest
 * inside an annotation highlight instead of cutting it in two.
 */
function inlineFaces(view: EditorView): DecorationSet {
  const { state } = view
  const out: Range<Decoration>[] = []
  for (const { from, to } of view.visibleRanges) {
    syntaxTree(state).iterate({
      from,
      to,
      enter: (node) => {
        if (node.name === 'InlineCode') {
          // Between the delimiters: the marks are the first and last children.
          let inFrom = node.from
          let inTo = node.to
          for (let c = node.node.firstChild; c; c = c.nextSibling) {
            if (c.name !== 'CodeMark') continue
            if (c.from === node.from) inFrom = c.to
            if (c.to === node.to) inTo = c.from
          }
          if (inTo > inFrom) out.push(markDeco('cm-md-inlinecode').range(inFrom, inTo))
          return
        }
        if (node.name !== 'Link') return
        if (!isInlineLink(state, node.node)) return
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

const inlineFacePlugin = ViewPlugin.fromClass(
  class {
    decorations: DecorationSet

    constructor(view: EditorView) {
      this.decorations = inlineFaces(view)
    }

    update(u: ViewUpdate) {
      if (u.view.composing) {
        if (u.docChanged) this.decorations = this.decorations.map(u.changes)
        return
      }
      if (u.docChanged || u.viewportChanged || syntaxTree(u.startState) !== syntaxTree(u.state)) {
        this.decorations = inlineFaces(u.view)
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
  return [makeLivePreviewPlugin(getParent), inlineFacePlugin]
}
