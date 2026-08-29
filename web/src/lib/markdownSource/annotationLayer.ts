import { StateEffect, StateField, type EditorState, type Extension, type Range } from '@codemirror/state'
import { Decoration, type DecorationSet, EditorView, WidgetType } from '@codemirror/view'
import { posFromSliceOffset } from './lineSep'

// The annotation layer over the markdown SOURCE surface: an open comment
// highlights its quoted passage, an open suggestion strikes what it replaces
// and shows the proposed text right after it.
//
// The file this replaced (lib/annotationDecorations.ts, deleted with the
// document-model surface) was 269 lines, and roughly 150 of them built a
// RENDERED projection of the document with a per-character map back to
// editor positions -- because the anchor was captured in that projection
// while the body was markdown source. Its Python counterpart
// (services/md_anchor.py) still builds that projection from the source with
// markdown-it, and says so in its docstring: it is what resolves the anchors
// written back then. Two implementations of one function, in two languages,
// that had to agree character for character.
//
// Here the document IS the markdown, so the anchor is a source span and
// locating it is `indexOf`. That is the entire mechanism, and the reason
// this file is short.
//
// Presentational, like every other layer on this surface: it adds
// decorations and never dispatches a document change.

export type AnnotationAnchor = {
  id: string
  kind: 'comment' | 'suggestion'
  status: string
  anchorQuote: string | null
  anchorPrefix: string | null
  anchorSuffix: string | null
  originalText: string | null
  proposedText: string | null
  /** Which projection the quote is written in. A 'rendered' anchor is not
   *  paintable here: its quote is a projection of the document, not a span of
   *  it, so `indexOf` would either miss or -- worse -- hit the wrong place. */
  anchorDomain?: 'source' | 'rendered'
}

/** The subset needed to find an annotation's passage. */
export type AnchorQuery = {
  kind: 'comment' | 'suggestion'
  anchorQuote: string | null
  anchorPrefix: string | null
  anchorSuffix: string | null
  originalText: string | null
  anchorDomain?: 'source' | 'rendered'
}

export type SourceRange = { from: number; to: number }

/** Push the current annotations onto the editor. */
export const setAnnotations = StateEffect.define<AnnotationAnchor[]>()
/** Pulse a range (the panel's "go to text"), or null to clear. */
export const flashAnnotation = StateEffect.define<SourceRange | null>()

function overlaps(r: SourceRange, used: SourceRange[]): boolean {
  return used.some((u) => r.from < u.to && u.from < r.to)
}

/**
 * Find an anchor's passage in the source.
 *
 * The prefix/suffix-anchored needle wins when it occurs somewhere not already
 * taken; a bare quote is the fallback, which is what lets two annotations
 * quoting the same words land on successive occurrences instead of stacking
 * on the first. `used` is what makes that ordering stable.
 *
 * Same rule as the server's `locate_source_span`, deliberately: what is
 * highlighted here has to be what accept will splice there, and the two
 * agreeing by construction is the point of having one domain.
 */
export function locateSourceAnchor(
  doc: string,
  a: AnchorQuery,
  used: SourceRange[] = [],
): SourceRange | null {
  // A rendered-domain anchor is a projection of the document, not a span of
  // it. Searching for it here would usually miss, and occasionally hit
  // something that merely looks the same -- which is worse than not painting.
  if (a.anchorDomain === 'rendered') return null
  const needle = a.kind === 'suggestion' ? a.originalText : a.anchorQuote
  if (!needle) return null
  const pfx = a.anchorPrefix ?? ''
  const sfx = a.anchorSuffix ?? ''
  const anchored = pfx + needle + sfx
  const useAnchored = anchored !== needle

  const scan = (target: string, offset: number): SourceRange | null => {
    let from = 0
    for (;;) {
      const idx = doc.indexOf(target, from)
      if (idx < 0) return null
      const s = idx + offset
      const cand = { from: s, to: s + needle.length }
      if (cand.to <= doc.length && !overlaps(cand, used)) return cand
      from = idx + 1
    }
  }
  return (useAnchored ? scan(anchored, pfx.length) : null) ?? scan(needle, 0)
}

/** The proposed replacement, shown after the struck original. */
class ProposedWidget extends WidgetType {
  readonly text: string
  readonly id: string

  constructor(id: string, text: string) {
    super()
    this.id = id
    this.text = text
  }

  // Keyed on the TEXT as well as the id: an edited proposal has to produce a
  // different widget, or CodeMirror reuses the DOM and the ghost text stays
  // stale.
  eq(other: WidgetType): boolean {
    return other instanceof ProposedWidget && other.id === this.id && other.text === this.text
  }

  toDOM(): HTMLElement {
    const span = document.createElement('span')
    span.className = 'anno-mark anno-mark--ins'
    span.textContent = this.text
    span.setAttribute('data-annotation-id', this.id)
    return span
  }

  ignoreEvent(): boolean {
    return true
  }
}

function build(
  state: EditorState,
  anchors: AnnotationAnchor[],
  flash: SourceRange | null,
): DecorationSet {
  const doc = state.sliceDoc()
  const out: Range<Decoration>[] = []
  // In SLICE offsets, which is the domain locateSourceAnchor searches and
  // compares in. Only what becomes a decoration is converted.
  const used: SourceRange[] = []
  const pos = (o: number) => posFromSliceOffset(state, o)

  // `flash` arrives in DOCUMENT positions already: the host locates the range
  // and converts before dispatching, because it also scrolls to it.
  if (flash && flash.to > flash.from && flash.to <= state.doc.length) {
    out.push(Decoration.mark({ class: 'anno-mark--flash' }).range(flash.from, flash.to))
  }

  for (const a of anchors) {
    if (a.status !== 'open') continue
    const found = locateSourceAnchor(doc, a, used)
    if (!found || found.to <= found.from) continue
    used.push(found)
    const r = { from: pos(found.from), to: pos(found.to) }
    if (r.to <= r.from) continue
    if (a.kind === 'comment') {
      out.push(
        Decoration.mark({
          class: 'anno-mark anno-mark--comment',
          attributes: { 'data-annotation-id': a.id },
        }).range(r.from, r.to),
      )
      continue
    }
    out.push(
      Decoration.mark({
        class: 'anno-mark anno-mark--del',
        attributes: { 'data-annotation-id': a.id },
      }).range(r.from, r.to),
    )
    const proposed = a.proposedText ?? ''
    if (proposed) {
      out.push(
        Decoration.widget({ widget: new ProposedWidget(a.id, proposed), side: 1 }).range(r.to),
      )
    }
  }
  return Decoration.set(out, true)
}

type LayerState = {
  anchors: AnnotationAnchor[]
  flash: SourceRange | null
  deco: DecorationSet
}

const annotationField = StateField.define<LayerState>({
  // Empty until the host pushes the annotations in: they live in React
  // state and arrive through a StateEffect.
  create: () => ({ anchors: [], flash: null, deco: Decoration.none }),
  update(value, tr) {
    let anchors = value.anchors
    let flash = value.flash
    let touched = false
    for (const e of tr.effects) {
      if (e.is(setAnnotations)) {
        anchors = e.value
        touched = true
      } else if (e.is(flashAnnotation)) {
        flash = e.value
        touched = true
      }
    }
    // A document edit invalidates a transient highlight rather than moving
    // it: the passage it pointed at may not be the same passage any more.
    if (tr.docChanged) flash = null
    if (!touched && !tr.docChanged) return value
    return { anchors, flash, deco: build(tr.state, anchors, flash) }
  },
  provide: (f) => EditorView.decorations.from(f, (v) => v.deco),
})

/** The annotation decoration layer. */
export function annotationLayer(): Extension {
  return [annotationField]
}

/** The anchors currently painted, in document order. What the toolbar's
 *  prev/next walks and what "go to text" resolves against. */
export function paintedAnchors(
  state: EditorState,
  anchors: AnnotationAnchor[],
): { anchor: AnnotationAnchor; range: SourceRange }[] {
  const doc = state.sliceDoc()
  const used: SourceRange[] = []
  const out: { anchor: AnnotationAnchor; range: SourceRange }[] = []
  for (const a of anchors) {
    const r = locateSourceAnchor(doc, a, used)
    if (!r) continue
    used.push(r)
    out.push({ anchor: a, range: r })
  }
  return out.sort((x, y) => x.range.from - y.range.from)
}
