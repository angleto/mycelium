import { StateField, type EditorState, type Extension, type Range } from '@codemirror/state'
import { Decoration, type DecorationSet, EditorView } from '@codemirror/view'
import { scanBlocks } from './blockScan'
import { overlaps, revealedRanges } from './reveal'
import { MathWidget, MermaidWidget, TableWidget } from './widgets'

// Block-level live preview: a mermaid fence as its diagram, a `$$` block as
// the typeset formula, a GFM table as a table, a setext underline folded
// into its heading.
//
// A StateField rather than a ViewPlugin, for two independent reasons.
//
// Block widgets declare `block: true`, which means they determine the height
// of the lines they replace. A ViewPlugin only sees the viewport, so heights
// would change as parsing and scrolling caught up and the document would
// jump under the reader.
//
// And CodeMirror flatly refuses a replace decoration that spans a line break
// from a plugin ("Decorations that replace line breaks may not be specified
// via plugins"), because that changes the line structure the view measures
// from. Every decoration here spans line breaks by definition.
//
// The same reveal rule as the inline layer: a block whose lines the
// selection touches shows its SOURCE, so editing is always over real
// markdown. For the two constructs where the preview is the point of writing
// them at all -- a diagram and a formula -- the preview stays rendered
// underneath while you edit the source, which is what the tiptap node view
// did and what makes them writable at all.

/** The rendered content of a fence, minus its fence lines. */
function fenceContent(state: EditorState, from: number, to: number): string {
  return to > from ? state.sliceDoc(from, to) : ''
}

function build(state: EditorState): DecorationSet {
  const revealed = revealedRanges(state)
  const out: Range<Decoration>[] = []

  for (const b of scanBlocks(state.doc)) {
    const shown = overlaps(revealed, b.from, b.to)

    if (b.kind === 'fence') {
      if (b.info !== 'mermaid') continue
      const code = fenceContent(state, b.contentFrom, b.contentTo)
      if (!code.trim()) continue
      const widget = new MermaidWidget(code)
      out.push(
        shown
          ? // Editing the source: keep the diagram below it, live. Losing the
            // preview the moment you click into the fence would make a
            // diagram something you can only write blind.
            Decoration.widget({ widget, block: true, side: 1 }).range(b.to)
          : Decoration.replace({ widget, block: true }).range(b.from, b.to),
      )
      continue
    }

    if (b.kind === 'math') {
      const widget = new MathWidget(b.tex)
      out.push(
        shown
          ? Decoration.widget({ widget, block: true, side: 1 }).range(b.to)
          : Decoration.replace({ widget, block: true }).range(b.from, b.to),
      )
      continue
    }

    if (b.kind === 'table') {
      if (shown) continue
      out.push(
        Decoration.replace({
          widget: new TableWidget(state.sliceDoc(b.from, b.to)),
          block: true,
        }).range(b.from, b.to),
      )
      continue
    }

    // setext: fold the underline INTO the heading by replacing the newline
    // before it as well, so the two source lines render as one heading line
    // instead of a heading followed by a blank.
    if (!shown && b.underlineFrom > 0) {
      out.push(Decoration.replace({}).range(b.underlineFrom - 1, b.to))
    }
  }

  return Decoration.set(out, true)
}

const blockField = StateField.define<DecorationSet>({
  create: (state) => build(state),
  update(deco, tr) {
    // Nothing that can change what is previewed: keep the set as is. Its
    // positions are still valid because neither the document nor the
    // selection moved.
    if (!tr.docChanged && !tr.selection) return deco
    return build(tr.state)
  },
  provide: (f) => EditorView.decorations.from(f),
})

/**
 * The block-level preview layer. Presentational: it never dispatches a
 * document change.
 *
 * No IME guard here, unlike the inline layer, and the reason is structural
 * rather than an omission: a block whose lines the selection touches is
 * never replaced, and composition happens at the selection. So this layer
 * never rebuilds the DOM the input method is composing into.
 */
export function blockPreview(): Extension {
  return [blockField]
}
