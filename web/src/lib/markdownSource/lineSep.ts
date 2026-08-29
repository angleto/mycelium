import type { EditorState } from '@codemirror/state'

// Line endings, which is the one place the source editor's byte-exactness
// is not free.
//
// CodeMirror splits the initial document once, at ``EditorState.create``,
// and rejoins it with ``state.lineBreak`` on every read. With no
// ``lineSeparator`` facet configured it splits on ``/\r\n?|\n/`` and rejoins
// with ``"\n"``, so a CRLF body would come back LF-normalised the moment
// anything read it. Configuring ``lineSeparator: '\r\n'`` fixes that, but it
// also means ``split('\r\n')``: a document whose endings are MIXED would
// collapse into a single CodeMirror line, and a single line has no block
// structure at all for the markdown parser to decorate.
//
// So the rule is: pin the separator only when the document is UNIFORMLY
// CRLF. Anything else runs on the default, which is exact for a uniformly
// LF document (the overwhelming majority) and normalises a mixed one to LF
// on the first edit. Opening a mixed body still writes nothing, because
// nothing reads the document until the user types.
//
// This is a stated limit rather than a discovered one, and it is strictly
// better than the surface it replaced, which destroyed every CRLF
// unconditionally (``a\r\nb`` round-tripped to ``a b``).

/**
 * The ``EditorState.lineSeparator`` value for ``src``: ``'\r\n'`` when every
 * line ending in the document is a CRLF, ``undefined`` otherwise (leave the
 * facet unset and take CodeMirror's default splitting).
 *
 * A document with no line ending at all is uniformly-anything, so it takes
 * the default: pinning CRLF there would be a guess about text the user has
 * not written yet.
 */
export function lineSepFor(src: string): '\r\n' | undefined {
  let lf = 0
  let crlf = 0
  for (let i = 0; i < src.length; i += 1) {
    if (src.charCodeAt(i) !== 10 /* \n */) continue
    lf += 1
    if (i > 0 && src.charCodeAt(i - 1) === 13 /* \r */) crlf += 1
  }
  return lf > 0 && lf === crlf ? '\r\n' : undefined
}

/**
 * Does ``src`` mix CRLF and LF endings? The editor cannot keep such a body
 * byte-exact (see above), so the host can say so rather than let the user
 * find out.
 */
export function hasMixedLineEndings(src: string): boolean {
  let lf = 0
  let crlf = 0
  for (let i = 0; i < src.length; i += 1) {
    if (src.charCodeAt(i) !== 10) continue
    lf += 1
    if (i > 0 && src.charCodeAt(i - 1) === 13) crlf += 1
  }
  return crlf > 0 && crlf < lf
}

/**
 * A slice offset as a document position.
 *
 * Anything that searches or diffs `state.sliceDoc()` works in STRING offsets.
 * CodeMirror counts a line break as ONE position whatever it is spelled as,
 * so in a CRLF body the two diverge by one per preceding line break, and
 * handing a string offset to `dispatch` addresses the wrong characters: an
 * annotation was painted two columns right of the words it quoted, and an
 * external-value sync spliced its change two columns off and corrupted the
 * body. Near the end of a long body the offset runs past `doc.length`
 * outright.
 *
 * The common case is the identity and says so: an LF body needs no walk.
 */
export function posFromSliceOffset(state: EditorState, off: number): number {
  if (state.lineBreak.length === 1) return off
  let consumed = 0
  for (let n = 1; n <= state.doc.lines; n += 1) {
    const line = state.doc.line(n)
    if (off <= consumed + line.length) return line.from + (off - consumed)
    consumed += line.length + state.lineBreak.length
    // The offset landed INSIDE the separator, which has no document position
    // of its own: clamp to the end of the line it follows.
    if (off < consumed) return line.to
  }
  return state.doc.length
}
