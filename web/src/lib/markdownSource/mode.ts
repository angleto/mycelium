import { Compartment, type Extension } from '@codemirror/state'
import type { EditorView } from '@codemirror/view'
import { blockPreview } from './blockPreview'
import { livePreview } from './livePreview'
import { sourceTheme, visualTheme } from './theme'
import type { ImageUploadParent } from '../imageUpload'

// The two ways of showing one document.
//
// This is a PRESENTATION switch and nothing else. Both modes hold the same
// CodeMirror state over the same markdown string; `state.sliceDoc()` returns
// the same bytes in either, there is no serializer on either side, and
// switching dispatches no document change. The retired pair of surfaces were
// two DOCUMENT MODELS, and the visual one could only save what its serializer
// produced -- which is why opening a body in it was safe and editing it once
// was not. Nothing here can be a data risk, because nothing here touches the
// document.
//
// What each mode is:
//
//   source  a text editor. Monospace, syntax highlighting, every character
//           where the author put it. No preview layers exist at all in this
//           configuration, so there is nothing that could hide or replace a
//           byte.
//
//   visual  the rendered view. Markup recedes except on the construct the
//           caret is in (reveal.ts), whole blocks are drawn as the diagram,
//           table, formula or picture they describe, and the typography is
//           the reader's.

export type MarkdownMode = 'source' | 'visual'

export const DEFAULT_MODE: MarkdownMode = 'visual'

/** Where the presentation lives in the extension array. */
export const markdownModeCompartment = new Compartment()

export type PresentationOptions = {
  /** Read LIVE by the image widget: a brand-new note has no id when its
   *  editor is built, and a bare-filename embed resolves against its parent. */
  getParent?: () => ImageUploadParent | undefined
}

/** The layers and the theme one mode installs. */
export function presentationFor(mode: MarkdownMode, opts: PresentationOptions = {}): Extension {
  if (mode === 'source') return [sourceTheme]
  return [livePreview({ getParent: opts.getParent }), blockPreview(), visualTheme]
}

/**
 * Switch a live editor between the two modes.
 *
 * A `Compartment.reconfigure`, never a `view.setState`: rebuilding the state
 * would drop the undo history, tear down and recreate every view plugin (the
 * attachment refcount goes to zero and every embedded image refetches), and,
 * on a body whose line endings are not uniform, silently rewrite the document
 * to LF while emitting nothing -- so the host would still be holding bytes
 * the editor had already discarded.
 *
 * The selection is re-asserted alongside. A reconfigure carries neither
 * `docChanged` nor `selectionSet`, so nothing would rebuild the decorations
 * or notify the annotation surface, and the annotation popover would be left
 * measuring coordinates from the typography that has just been replaced.
 * There is no `changes` here, so this is not a document change.
 */
export function setMarkdownMode(
  view: EditorView,
  mode: MarkdownMode,
  opts: PresentationOptions = {},
): void {
  view.dispatch({
    effects: markdownModeCompartment.reconfigure(presentationFor(mode, opts)),
    selection: view.state.selection,
  })
}
