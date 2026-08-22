import type { Editor as TiptapEditor } from '@tiptap/core'
import type { EditorView as CmView } from '@codemirror/view'
import type { AnchorDomain } from './annotationsApi'
import { readSourceSelection } from './markdownSource/sourceSelection'

// What the inline annotation UI actually needs from an editing surface.
//
// `InlineAnnotator` is 613 lines and touches the editor in exactly three of
// them: it reads the selection, it subscribes to selection changes, and it
// listens for a click on an inline mark. Everything else -- the popovers, the
// cards, the accept/reject/resolve calls, the whole 200-line JSX block --
// never names the editor at all.
//
// So this is the seam, rather than a second copy of the component for the
// second surface. Two adapters implement it: the legacy tiptap one, which
// reads a RENDERED projection of the document, and the markdown source one,
// whose selection already IS a span of the stored bytes.
//
// The `domain` field is not decoration. The two adapters capture anchors in
// two different languages, and the row records which one it was (migration
// 0099): a quote read in the wrong domain does not merely fail to locate, it
// can match the WRONG passage.

/** A selection, ready to become an annotation anchor. */
export type SurfaceSelection = {
  from: number
  to: number
  text: string
  prefix: string
  suffix: string
  /** Viewport coordinates, for the fixed-position popovers. */
  left: number
  top: number
  bottom: number
}

export type AnnotationSurface = {
  /** The live selection as an anchor, or null when there is nothing to
   *  annotate. Read from the editor's STATE, never from a DOM selection: a
   *  blur must not be able to collapse it before a handler runs. */
  readSelection: () => SurfaceSelection | null
  /** Subscribe to selection (and document) changes; returns the
   *  unsubscribe. */
  onSelectionChange: (cb: () => void) => () => void
  /** The element that carries the inline marks, for click delegation. */
  markRoot: () => HTMLElement | null
  /** Which projection `readSelection().text` is written in. */
  domain: AnchorDomain
}

/**
 * The legacy WYSIWYG surface.
 *
 * Its quote is `doc.textBetween(from, to, ' ')`: markup stripped, links
 * reduced to their label, blocks joined by a space. That is a projection of
 * the document rather than a span of it, which is why the server has to keep
 * a matching projection to resolve it, and why this adapter declares
 * `rendered`.
 */
export function tiptapSurface(editor: TiptapEditor): AnnotationSurface {
  return {
    domain: 'rendered',
    markRoot: () => editor.view.dom as HTMLElement,
    onSelectionChange: (cb) => {
      editor.on('selectionUpdate', cb)
      return () => editor.off('selectionUpdate', cb)
    },
    readSelection: () => {
      const { state, view } = editor
      const { from, to, empty } = state.selection
      if (empty || to <= from) return null
      const doc = state.doc
      const text = doc.textBetween(from, to, ' ')
      if (!text.trim()) return null
      const prefix = doc.textBetween(doc.resolve(from).start(), from, ' ').slice(-24)
      const suffix = doc.textBetween(to, doc.resolve(to).end(), ' ').slice(0, 24)
      try {
        const a = view.coordsAtPos(from)
        const b = view.coordsAtPos(to)
        return {
          from,
          to,
          text,
          prefix,
          suffix,
          left: (a.left + b.left) / 2,
          top: Math.min(a.top, b.top),
          bottom: Math.max(a.bottom, b.bottom),
        }
      } catch {
        return null
      }
    },
  }
}

/**
 * The markdown source surface.
 *
 * Its quote is `sliceDoc(from, to)`: the stored bytes. The heavy lifting --
 * trimming edge whitespace, growing out of a half-covered delimiter run,
 * taking each end's context from its own block -- lives in
 * `markdownSource/sourceSelection.ts`, because it is a pure function of the
 * document and deserves to be tested as one.
 */
export function sourceSurface(view: CmView): AnnotationSurface {
  return {
    domain: 'source',
    markRoot: () => view.contentDOM,
    onSelectionChange: (cb) => {
      // CodeMirror has no event emitter; the host wires an updateListener and
      // fans out through this registry. Kept here so the surface contract is
      // the same shape for both editors.
      return subscribeSelection(view, cb)
    },
    readSelection: () => {
      const sel = readSourceSelection(view)
      if (!sel || !sel.coords) return null
      return {
        from: sel.from,
        to: sel.to,
        text: sel.text,
        prefix: sel.prefix,
        suffix: sel.suffix,
        ...sel.coords,
      }
    },
  }
}

// --- selection fan-out for CodeMirror ---------------------------------------
//
// A CodeMirror extension is fixed at state creation, so a subscriber that
// arrives later (this component mounts after the editor) cannot add one. The
// editor installs ONE updateListener that calls `notifySelection`, and
// listeners register here.

const listeners = new WeakMap<CmView, Set<() => void>>()

function subscribeSelection(view: CmView, cb: () => void): () => void {
  let set = listeners.get(view)
  if (!set) {
    set = new Set()
    listeners.set(view, set)
  }
  set.add(cb)
  return () => {
    set?.delete(cb)
  }
}

/** Called by the source editor's update listener on every selection or
 *  document change. */
export function notifySelection(view: CmView): void {
  const set = listeners.get(view)
  if (!set) return
  for (const cb of Array.from(set)) cb()
}
