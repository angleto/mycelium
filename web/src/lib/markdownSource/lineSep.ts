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
