import { EditorState, Transaction, type Extension } from '@codemirror/state'
import type { EditorView } from '@codemirror/view'
import { lineSepFor, posFromSliceOffset } from './lineSep'

// Installing an externally-changed body onto a live editor.
//
// Its own module rather than a helper inside the React shell, because it is
// the one path that writes to the document without a person having typed
// anything -- a conflict reload, an accepted suggestion, a different note
// part loading -- and it is a pure function of (view, next) that a test can
// drive directly.

/**
 * Push an externally-changed ``next`` onto a live view as the smallest
 * change that produces it.
 *
 * Two things are load-bearing here.
 *
 * ``Transaction.addToHistory.of(false)``: an external value is not something
 * the user did, so it must not enter the undo stack. Without it, accepting a
 * suggestion (which reloads the body from the server) becomes undoable, and
 * one Cmd+Z would write the pre-accept body back over the accepted one while
 * the annotation stayed ``accepted``.
 *
 * The ``setState`` branch: ``lineSeparator`` is read once, when the state is
 * created, and later inserts split on the CURRENT ``state.lineBreak``. It
 * cannot be swapped by reconfiguring. So when the incoming body wants a
 * different separator than the live one, the state is rebuilt rather than
 * patched.
 *
 * ``onRebuilt`` closes the hole that rebuild opens. A ``setState`` is not a
 * document change, so the update listener stays quiet -- and a body that
 * MIXES line endings does not survive the rebuild unchanged (it normalises to
 * LF, which is the stated policy). Without this the host would go on holding
 * bytes the editor had already discarded, and the next single keystroke would
 * emit the whole rewritten body and autosave it as if it were a
 * one-character edit.
 */
export function syncExternal(
  view: EditorView,
  next: string,
  extensionsFor: (src: string) => Extension[],
  onRebuilt: (text: string) => void,
): void {
  const cur = view.state.sliceDoc()
  if (cur === next) return
  const wanted = lineSepFor(next) ?? '\n'
  const live = view.state.lineBreak
  if (wanted !== live) {
    view.setState(EditorState.create({ doc: next, extensions: extensionsFor(next) }))
    const after = view.state.sliceDoc()
    if (after !== next) onRebuilt(after)
    return
  }
  // Common prefix / suffix, so a one-character edit dispatches a
  // one-character change and the selection of anyone else looking at this
  // document maps through it instead of being reset.
  let a = 0
  const max = Math.min(cur.length, next.length)
  while (a < max && cur[a] === next[a]) a += 1
  let b = 0
  while (b < max - a && cur[cur.length - 1 - b] === next[next.length - 1 - b]) b += 1
  // Those are STRING offsets into `cur`; a change is addressed in DOCUMENT
  // positions, and the two differ by one per preceding line break in a CRLF
  // body. Dispatching the raw offsets spliced the change that many columns to
  // the left, so an external update (a conflict reload, an accepted
  // suggestion) corrupted the body it was supposed to install.
  view.dispatch({
    changes: {
      from: posFromSliceOffset(view.state, a),
      to: posFromSliceOffset(view.state, cur.length - b),
      insert: next.slice(a, next.length - b),
    },
    annotations: Transaction.addToHistory.of(false),
  })
}
