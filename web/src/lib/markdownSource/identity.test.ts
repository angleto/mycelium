import { describe, expect, it } from 'vitest'
import { EditorState } from '@codemirror/state'
import { EditorView } from '@codemirror/view'
import { hasMixedLineEndings, lineSepFor } from './lineSep'
import { markdownSourceExtensions } from './extensions'

// The contract of the source editor, asserted as a law rather than a habit:
// the document IS the markdown, so reading it back gives the bytes it was
// given. Every fixture in test/markdown-corpus is a construct the previous
// (tiptap) surface rewrote or corrupted -- hard-wrapped prose, padded and
// aligned tables, `*` and `+` bullets, `~~~` fences, indented code, setext
// headings, reference links, footnotes, front matter, raw HTML, an escaped
// pipe inside a table cell. There is no KNOWN_LOSSY allowlist here, and
// there is not meant to be one: a fixture that cannot round-trip is a bug,
// not an entry.
//
// The .md files are pinned `-text` in .gitattributes so a checkout cannot
// rewrite their line endings and make this pass for the wrong reason.

const fixtures = import.meta.glob('../../../test/markdown-corpus/*.md', {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>

const named = Object.entries(fixtures).map(([path, src]) => ({
  name: path.slice(path.lastIndexOf('/') + 1),
  src,
}))

// The REAL extension set, not a hand-rolled subset: if the line-separator
// configuration were dropped from extensions.ts, a test that rebuilt it here
// would keep passing while the editor started eating CRLF.
function stateFor(src: string): EditorState {
  return EditorState.create({
    doc: src,
    extensions: markdownSourceExtensions({ src, onChange: () => {} }),
  })
}

describe('the corpus is actually loaded', () => {
  // A glob that matches nothing makes every assertion below vacuous, which
  // is the classic way a byte-level test passes while testing nothing.
  it('has fixtures, including the ones that used to break', () => {
    expect(named.length).toBeGreaterThanOrEqual(20)
    const names = named.map((f) => f.name)
    for (const required of [
      'verbatim-repo-note.md',
      'tables-alignment.md',
      'frontmatter.md',
      'footnotes.md',
      'fences.md',
      'raw-html.md',
    ]) {
      expect(names).toContain(required)
    }
    for (const f of named) expect(f.src.length).toBeGreaterThan(0)
  })
})

describe('every fixture is a fixed point of the document model', () => {
  it.each(named.map((f) => [f.name, f.src] as const))('%s', (_name, src) => {
    expect(stateFor(src).sliceDoc()).toBe(src)
  })
})

describe('line endings', () => {
  const LF = 'riga uno\nriga due\n'
  const CRLF = 'riga uno\r\nriga due\r\n'
  const MIXED = 'riga uno\r\nriga due\nriga tre\r\n'

  it('a uniformly-LF document is exact', () => {
    expect(lineSepFor(LF)).toBeUndefined()
    expect(stateFor(LF).sliceDoc()).toBe(LF)
  })

  it('a uniformly-CRLF document is exact', () => {
    expect(lineSepFor(CRLF)).toBe('\r\n')
    expect(stateFor(CRLF).sliceDoc()).toBe(CRLF)
  })

  it('a document with no line ending at all takes the default', () => {
    expect(lineSepFor('una riga sola')).toBeUndefined()
    expect(stateFor('una riga sola').sliceDoc()).toBe('una riga sola')
  })

  it('a MIXED document normalises to LF, and that is the stated policy', () => {
    // This is the one expectation in this file that is a decision rather
    // than a law. Pinning '\r\n' for a mixed body would make CodeMirror
    // split on '\r\n' only, collapsing the whole document into one line
    // with no block structure for the markdown parser to work on -- worse
    // than normalising. The previous surface destroyed ALL CRLF
    // unconditionally, so this is strictly better and deliberately narrow.
    expect(hasMixedLineEndings(MIXED)).toBe(true)
    expect(lineSepFor(MIXED)).toBeUndefined()
    expect(stateFor(MIXED).sliceDoc()).toBe('riga uno\nriga due\nriga tre\n')
  })

  it('doc.toString() is NOT the emit path, and this is why', () => {
    // Text.toString() is sliceString(0), whose lineSep defaults to '\n';
    // only EditorState.sliceDoc passes state.lineBreak. Emitting through
    // toString() would silently LF-normalise a CRLF body. Asserted so the
    // difference cannot be "simplified" away later.
    const st = stateFor(CRLF)
    expect(st.doc.toString()).toBe('riga uno\nriga due\n')
    expect(st.sliceDoc()).toBe(CRLF)
  })
})

describe('an edit changes only what was edited', () => {
  it('typing one character leaves every other byte alone', () => {
    const src = fixtures[
      Object.keys(fixtures).find((k) => k.endsWith('verbatim-repo-note.md')) as string
    ]
    const at = src.indexOf('paragrafo')
    expect(at).toBeGreaterThan(0)
    const st = stateFor(src)
    const after = st.update({ changes: { from: at, insert: 'X' } }).state
    expect(after.sliceDoc()).toBe(src.slice(0, at) + 'X' + src.slice(at))
  })
})

describe('the emit contract', () => {
  it('mounting an editor over a body emits nothing', () => {
    // The property the note-parts autosave depends on: opening a body must
    // not look like an edit. It is what stopped the previous surface from
    // silently rewriting verbatim content on open, and it has to keep
    // holding once decorations and widgets are added on top.
    const src = 'riga uno,\nriga due.\n\n| a | b |\n|---|---|\n| 1 | 2 |\n'
    const emitted: string[] = []
    const view = new EditorView({
      state: EditorState.create({
        doc: src,
        extensions: markdownSourceExtensions({
          src,
          onChange: (v) => emitted.push(v),
        }),
      }),
    })
    try {
      expect(emitted).toEqual([])
      expect(view.state.sliceDoc()).toBe(src)
      // A selection move is not an edit either.
      view.dispatch({ selection: { anchor: 3 } })
      expect(emitted).toEqual([])
      // A real edit emits exactly once, with the new bytes.
      view.dispatch({ changes: { from: 0, insert: '# ' } })
      expect(emitted).toEqual(['# ' + src])
    } finally {
      view.destroy()
    }
  })
})
