import { afterEach, describe, expect, it } from 'vitest'
import { EditorSelection, EditorState } from '@codemirror/state'
import { EditorView } from '@codemirror/view'
import { forceParsing } from '@codemirror/language'
import { markdownSourceExtensions } from './extensions'
import {
  activeMarks,
  insertHorizontalRule,
  insertTable,
  setLink,
  toggleBulletList,
  toggleCodeBlock,
  toggleHeading,
  toggleOrderedList,
  toggleQuote,
  toggleTaskList,
  toggleWrap,
} from './commands'
import { addColumnAfter, addRowAfter, deleteTable, formatTable, nextCell } from './tableCommands'
import { splitRow } from './widgets'
import { hasMixedLineEndings } from './lineSep'

// Every toolbar command is now a total function from (source, selection) to
// source, so these assert EXACT BYTES. That is the point of the substrate:
// there is nothing between the button and the characters, so there is
// nothing to inspect except the characters.
//
// Selections are written with «guillemets» in the fixture, which keeps the
// intent readable next to the expectation.

const views: EditorView[] = []

function parse(marked: string): { doc: string; from: number; to: number } {
  const from = marked.indexOf('«')
  const to = marked.indexOf('»')
  if (from < 0) return { doc: marked, from: marked.length, to: marked.length }
  const doc = marked.replace('«', '').replace('»', '')
  return { doc, from, to: to < 0 ? from : to - 1 }
}

function open(marked: string): EditorView {
  const { doc, from, to } = parse(marked)
  const view = new EditorView({
    state: EditorState.create({
      doc,
      extensions: markdownSourceExtensions({ src: doc, onChange: () => {} }),
      selection: EditorSelection.range(from, to),
    }),
  })
  forceParsing(view, doc.length, 5_000)
  views.push(view)
  return view
}

function run(marked: string, cmd: (v: EditorView) => boolean): { doc: string; ok: boolean } {
  const view = open(marked)
  const ok = cmd(view)
  return { doc: view.state.sliceDoc(), ok }
}

afterEach(() => {
  while (views.length) views.pop()?.destroy()
})

describe('inline wrapping', () => {
  it('wraps a selection', () => {
    expect(run('un «grassetto» qui', toggleWrap('bold')).doc).toBe('un **grassetto** qui')
    expect(run('un «corsivo» qui', toggleWrap('italic')).doc).toBe('un _corsivo_ qui')
    expect(run('un «via» qui', toggleWrap('strike')).doc).toBe('un ~~via~~ qui')
    expect(run('un «x» qui', toggleWrap('code')).doc).toBe('un `x` qui')
  })

  it('unwraps when the selection IS the wrapped text', () => {
    expect(run('un «**grassetto**» qui', toggleWrap('bold')).doc).toBe('un grassetto qui')
  })

  it('unwraps when the delimiters hug the selection', () => {
    expect(run('un **«grassetto»** qui', toggleWrap('bold')).doc).toBe('un grassetto qui')
  })

  it('recognises the OTHER spelling when removing', () => {
    // Bodies written by the previous editor use `*em*`; the italic button
    // has to un-toggle those, not add a second pair around them.
    expect(run('un *«corsivo»* qui', toggleWrap('italic')).doc).toBe('un corsivo qui')
    expect(run('un __«bold»__ qui', toggleWrap('bold')).doc).toBe('un bold qui')
  })

  it('on an empty selection wraps the WORD under the caret', () => {
    // Not `add + add` with the caret parked between them: an empty pair is
    // not emphasis in CommonMark. `****` mid-line is four literal asterisks
    // and, on a line of its own, a horizontal rule -- neither of which is
    // what pressing B asked for, and neither explicable once the markup is
    // drawn rather than shown.
    const view = open('scrivi cia«»o qui')
    expect(toggleWrap('bold')(view)).toBe(true)
    expect(view.state.sliceDoc()).toBe('scrivi **ciao** qui')
    // The caret stays where it was, now inside the wrap.
    expect(view.state.selection.main.head).toBe(12)
  })

  it('un-wraps from a caret inside an already-wrapped word', () => {
    const view = open('un **grasse«»tto** qui')
    expect(toggleWrap('bold')(view)).toBe(true)
    expect(view.state.sliceDoc()).toBe('un grassetto qui')
  })

  it('REFUSES an empty selection with no word under the caret', () => {
    const src = 'scrivi «» qui'
    const { doc, ok } = run(src, toggleWrap('bold'))
    expect(ok).toBe(false)
    expect(doc).toBe(parse(src).doc)
    // On an empty line it used to write `****`, which is a HORIZONTAL RULE.
    const empty = run('«»', toggleWrap('bold'))
    expect(empty.ok).toBe(false)
    expect(empty.doc).toBe('')
  })

  it('italic inside a bold run adds, it does not eat one asterisk', () => {
    // `**grassetto**` is a RUN of two asterisks. Looking for "a `*` before
    // and a `*` after" finds one, and taking one from each end would turn
    // bold into italic. The parser knows which run belongs to which node.
    expect(run('un **«grassetto»** qui', toggleWrap('italic')).doc).toBe(
      'un **_grassetto_** qui',
    )
  })

  it('the code button can REMOVE the span it reports itself pressed inside', () => {
    const view = open('un `cod«»ice` qui')
    expect(activeMarks(view.state).has('code')).toBe(true)
    expect(toggleWrap('code')(view)).toBe(true)
    expect(view.state.sliceDoc()).toBe('un codice qui')
  })

  it('the code-block button removes the fence it is inside', () => {
    expect(run('```\nlet «x» = 1\n```\n', toggleCodeBlock).doc).toBe('let x = 1\n')
    // An unterminated fence has only an opener to take off, and its last
    // CONTENT line is not a closer to be deleted.
    expect(run('```\nlet «x» = 1\n', toggleCodeBlock).doc).toBe('let x = 1\n')
    // A `~~~` line inside a ``` fence is content, not the closer.
    expect(run('```\n«~~~»\n', toggleCodeBlock).doc).toBe('~~~\n')
    // From the FIRST COLUMN of the opening fence line. Asking the syntax tree
    // with side -1 resolved into the block BEFORE the fence and answered
    // "not in a fence", so the button nested a second one inside the first.
    expect(run('prima\n\n«»```\nlet x = 1\n```\n', toggleCodeBlock).doc).toBe(
      'prima\n\nlet x = 1\n',
    )
  })

  it('trims the selection, so padding cannot become literal asterisks', () => {
    // `** parola **` is not emphasis: a `**` followed by a space is not
    // left-flanking. Wrapping a selection that includes its own padding wrote
    // four literal asterisks -- and with no node for the un-toggle to find,
    // pressing the button again added another pair, and again, without bound.
    // Selecting a word plus its trailing space is an ordinary drag.
    expect(run('un «parola »qui', toggleWrap('bold')).doc).toBe('un **parola** qui')
    expect(run('un« parola» qui', toggleWrap('bold')).doc).toBe('un **parola** qui')
    // And it is a TOGGLE again: twice is the identity.
    const view = open('un «parola »qui')
    toggleWrap('bold')(view)
    view.dispatch({ selection: { anchor: 5, head: 11 } })
    toggleWrap('bold')(view)
    expect(view.state.sliceDoc()).toBe('un parola qui')
  })

  it('REFUSES against an adjacent run of the same delimiter', () => {
    // `**Nota**seguito` bolding `seguito` gives `**Nota****seguito**`, which
    // parses as ONE StrongEmphasis whose content is the literal
    // `Nota****seguito`: the middle asterisks are text and the first word has
    // quietly stopped being bold. There is no spelling that works.
    const src = '**Nota**segu«»ito'
    const { doc, ok } = run(src, toggleWrap('bold'))
    expect(ok).toBe(false)
    expect(doc).toBe(parse(src).doc)
  })

  it('REFUSES inside a link destination, which is not prose', () => {
    // With no selection the caret expands to the word under it, and a word
    // inside a URL is part of the URL: wrapping it does not emphasise
    // anything, it rewrites the address.
    for (const src of [
      '![alt](/attachments/abc«»123/download)',
      '[x](@task:aaaa«»bbbb)',
      '[etichetta](https://esem«»pio.it "titolo")',
    ]) {
      const { doc, ok } = run(src, toggleWrap('bold'))
      expect(ok).toBe(false)
      expect(doc).toBe(parse(src).doc)
    }
  })

  it('REFUSES a selection that crosses a blank line', () => {
    // One pair of delimiters cannot wrap two paragraphs: the opener would
    // close against text in the next one.
    const src = 'primo «paragrafo\n\nsecondo» paragrafo'
    const { doc, ok } = run(src, toggleWrap('bold'))
    expect(ok).toBe(false)
    expect(doc).toBe(parse(src).doc)
  })

  it('REFUSES inside a fenced code block', () => {
    const src = '```\nlet «x» = 1\n```\n'
    const { doc, ok } = run(src, toggleWrap('bold'))
    expect(ok).toBe(false)
    expect(doc).toBe(parse(src).doc)
  })

  it('REFUSES inside a table delimiter row', () => {
    const src = '| a | b |\n| «---» | --- |\n| 1 | 2 |\n'
    const { ok } = run(src, toggleWrap('bold'))
    expect(ok).toBe(false)
  })
})

describe('line prefixes', () => {
  it('toggles a heading, replacing a different level rather than stacking', () => {
    expect(run('«Titolo»', toggleHeading(1)).doc).toBe('# Titolo')
    expect(run('«# Titolo»', toggleHeading(1)).doc).toBe('Titolo')
    expect(run('«# Titolo»', toggleHeading(2)).doc).toBe('## Titolo')
    expect(run('«### Titolo»', toggleHeading(2)).doc).toBe('## Titolo')
  })

  it('toggles a bullet list over every selected line', () => {
    expect(run('«uno\ndue\ntre»', toggleBulletList).doc).toBe('- uno\n- due\n- tre')
    expect(run('«- uno\n- due»', toggleBulletList).doc).toBe('uno\ndue')
  })

  it('numbers an ordered list from one', () => {
    expect(run('«uno\ndue\ntre»', toggleOrderedList).doc).toBe('1. uno\n2. due\n3. tre')
  })

  it('numbers from one even when the selection starts on a blank line', () => {
    // A blank line is skipped, but it used to occupy an index, so the first
    // real item came out as `2.`.
    expect(run('«\nuno\ndue»', toggleOrderedList).doc).toBe('\n1. uno\n2. due')
  })

  it('switches list kind instead of stacking markers', () => {
    expect(run('«- uno\n- due»', toggleOrderedList).doc).toBe('1. uno\n2. due')
    expect(run('«1. uno\n2. due»', toggleTaskList).doc).toBe('- [ ] uno\n- [ ] due')
    expect(run('«- [ ] uno»', toggleBulletList).doc).toBe('- uno')
  })

  it('toggles a quote', () => {
    expect(run('«uno\ndue»', toggleQuote).doc).toBe('> uno\n> due')
    expect(run('«> uno\n> due»', toggleQuote).doc).toBe('uno\ndue')
  })

  it('leaves a blank line alone when adding a list marker', () => {
    expect(run('«uno\n\ndue»', toggleBulletList).doc).toBe('- uno\n\n- due')
  })
})

describe('block inserts', () => {
  it('puts a rule on its own block', () => {
    expect(run('testo«»', insertHorizontalRule).doc).toBe('testo\n\n---\n')
    expect(run('«»', insertHorizontalRule).doc).toBe('---\n')
  })

  it('fences a selection', () => {
    expect(run('«let x = 1»', toggleCodeBlock).doc).toBe('```\nlet x = 1\n```\n')
  })

  it('inserts a table with a delimiter row', () => {
    const { doc } = run('«»', insertTable)
    expect(doc.split('\n')[1]).toBe('| --- | --- | --- |')
  })
})

describe('links', () => {
  it('wraps a selection, escaping the label', () => {
    expect(run('vedi «la ] guida» ok', (v) => setLink(v, () => 'https://e.com')).doc).toBe(
      String.raw`vedi [la \] guida](https://e.com) ok`,
    )
  })

  it('replaces the destination of the link the caret is in', () => {
    expect(
      run('vedi [la guida](https://old.com«») ok', (v) => setLink(v, () => 'https://new.com'))
        .doc,
    ).toBe('vedi [la guida](https://new.com) ok')
  })

  it('an empty destination unwraps to the bare label', () => {
    expect(run('vedi [la guida](https://old.com«») ok', (v) => setLink(v, () => '')).doc).toBe(
      'vedi la guida ok',
    )
  })

  it('a cancelled prompt changes nothing', () => {
    const src = 'vedi «la guida» ok'
    expect(run(src, (v) => setLink(v, () => null)).doc).toBe(parse(src).doc)
  })

  it('a bare bracket pair is NOT a link to write a destination into', () => {
    // lezer emits a `Link` node for any bracket run. Taking those for links
    // spliced the URL in before the `]` and produced no link at all.
    expect(run('see «[proven]» status', (v) => setLink(v, () => 'https://x')).doc).toBe(
      String.raw`see [\[proven\]](https://x) status`,
    )
    expect(run('vedi «array[0]» qui', (v) => setLink(v, () => 'https://x')).doc).toBe(
      String.raw`vedi [array\[0\]](https://x) qui`,
    )
    expect(run('«[a][ref]»', (v) => setLink(v, () => 'https://x')).doc).toBe(
      String.raw`[\[a\]\[ref\]](https://x)`,
    )
  })

  it('an inline link with an EMPTY destination still takes one', () => {
    expect(run('vedi [la guida](«») ok', (v) => setLink(v, () => 'https://x')).doc).toBe(
      'vedi [la guida](https://x) ok',
    )
  })

  it('is a fixed point: pressing it twice writes what pressing it once did', () => {
    // The label comes out of the DOCUMENT, so it is already escaped. Running
    // the raw-text escaper over it doubled every backslash, and every press
    // doubled them again -- backslashes multiplying through a body is the
    // exact failure the previous editor was retired for.
    const once = run(String.raw`«a \] b»`, (v) => setLink(v, () => 'https://x')).doc
    expect(once).toBe(String.raw`[a \] b](https://x)`)
    const view = open(String.raw`«a \] b»`)
    setLink(view, () => 'https://x')
    view.dispatch({ selection: { anchor: 0, head: view.state.doc.length } })
    setLink(view, () => 'https://x')
    expect(view.state.sliceDoc()).toBe(once)
  })

  it('a label ending in a backslash still comes out a link', () => {
    // A dangling backslash would escape the `]` the emitter appends, and the
    // result parses as text with no Link node -- and no way to repair it with
    // the same button, since there would be no link for it to find.
    const view = open(String.raw`«C:\Users\»`)
    setLink(view, () => 'https://e.example')
    const doc = view.state.sliceDoc()
    expect(doc).toBe(String.raw`[C:\Users\\](https://e.example)`)
    forceParsing(view, doc.length, 5_000)
    // It really is a link, not text that looks like one.
    expect([...activeMarks(view.state)]).toContain('link')
  })

  it('REFUSES a selection that spans more than one line', () => {
    // A label may legally wrap, but the emitter collapses the newline to a
    // space, which changes bytes outside the construct that was selected.
    const src = '«riga uno\nriga due»'
    const { doc, ok } = run(src, (v) => setLink(v, () => 'https://x'))
    expect(ok).toBe(false)
    expect(doc).toBe(parse(src).doc)
  })
})

describe('tables', () => {
  const TABLE = '| a | b |\n| --- | --- |\n| 1 | 2 |\n'

  it('adds a row below the caret, never above the delimiter', () => {
    expect(run('| a«» | b |\n| --- | --- |\n| 1 | 2 |\n', addRowAfter).doc).toBe(
      '| a | b |\n| --- | --- |\n|  |  |\n| 1 | 2 |\n',
    )
  })

  it('adds a column to every row, delimiter included', () => {
    expect(run('| a«» | b |\n| --- | --- |\n| 1 | 2 |\n', addColumnAfter).doc).toBe(
      '| a | b |   |\n| --- | --- | --- |\n| 1 | 2 |   |\n',
    )
  })

  it('deletes the whole table', () => {
    expect(run('prima\n\n| a«» | b |\n| --- | --- |\n| 1 | 2 |\n\ndopo\n', deleteTable).doc).toBe(
      'prima\n\ndopo\n',
    )
  })

  it('Tab selects the next cell without reformatting anything', () => {
    const view = open('| a«» | b |\n| --- | --- |\n| 1 | 2 |\n')
    expect(nextCell(view)).toBe(true)
    expect(view.state.sliceDoc()).toBe(TABLE)
    expect(view.state.sliceDoc(view.state.selection.main.from, view.state.selection.main.to)).toBe(
      'b',
    )
  })

  it('formatTable re-pads AND keeps the alignment markers', () => {
    // The previous editor dropped these on every edit. Here they survive a
    // deliberate reformat, which is the only time bytes are rewritten.
    const { doc } = run('| a«» | lungo |\n| :-- | --: |\n| 1 | 2 |\n', formatTable)
    expect(doc.split('\n')[1]).toBe('| :--- | ----: |')
    // The content columns are padded to the DELIMITER's width too, or the
    // re-pad would leave `:---` sticking out past a one-character column.
    expect(doc.split('\n')[0]).toBe('| a    | lungo |')
    expect(doc.split('\n')[2]).toBe('| 1    | 2     |')
  })

  it('formatTable keeps an ESCAPED pipe escaped', () => {
    // `splitRow` unescapes `\|` so a cell reads as its text; joining that
    // back with ` | ` separated on it, the row gained a cell against a
    // two-column delimiter, and the last cell was destroyed. That is the
    // corruption the retired serializer shipped, reached from a button.
    const src = String.raw`| pipe | note |` + '\n| --- | --- |\n' + String.raw`| a \| b | escaped |` + '\n'
    const { doc } = run(src.replace('| pipe', '| pi«»pe'), formatTable)
    const rows = doc.split('\n')
    expect(splitRow(rows[2])).toEqual(['a | b', 'escaped'])
    expect(rows[2]).toContain(String.raw`a \| b`)
    // Idempotent: reformatting the reformatted table changes nothing.
    const twice = run(doc.replace('| pipe', '| pi«»pe'), formatTable).doc
    expect(twice).toBe(doc)
  })

  it('nothing table-shaped fires outside a table', () => {
    expect(run('solo testo«»', addRowAfter).ok).toBe(false)
    expect(run('solo testo«»', nextCell).ok).toBe(false)
    expect(run('solo testo«»', deleteTable).ok).toBe(false)
  })
})

describe('a CRLF body stays uniformly CRLF', () => {
  // CodeMirror splits an inserted string on the document's OWN separator, so
  // a `\n` written into a CRLF body survives as a literal character inside a
  // line. The document then MIXES its line endings, which is the one shape
  // this editor cannot keep byte-exact.
  const CRLF = '| a | b |\r\n| --- | --- |\r\n| 1 | 2 |\r\ndopo\r\n'

  const cases: [string, (v: EditorView) => boolean][] = [
    ['formatTable', formatTable],
    ['addRowAfter', addRowAfter],
    ['addColumnAfter', addColumnAfter],
    ['insertTable', insertTable],
    ['insertHorizontalRule', insertHorizontalRule],
    ['toggleCodeBlock', toggleCodeBlock],
  ]

  it.each(cases)('%s', (_name, cmd) => {
    const view = open(CRLF.replace('| a', '| a«»'))
    cmd(view)
    const doc = view.state.sliceDoc()
    expect(hasMixedLineEndings(doc)).toBe(false)
    expect(doc).not.toContain('\r\r')
  })

  it('a table command reads CRLF rows as rows, not as rows with a stray CR', () => {
    // `sliceDoc` hands back CRLF; splitting that on `\n` left a `\r` on the
    // end of every line, which then travelled into the cell text, the
    // measured widths, and everything rewritten from them.
    const view = open(CRLF.replace('| 1', '| 1«»'))
    addColumnAfter(view)
    const rows = view.state.sliceDoc().split('\r\n')
    expect(splitRow(rows[0])).toEqual(['a', 'b', ''])
    expect(splitRow(rows[2])).toEqual(['1', '2', ''])
  })
})

describe('activeMarks', () => {
  it('reports the constructs the caret is inside', () => {
    const view = open('## Titolo«»')
    expect([...activeMarks(view.state)]).toContain('heading2')
  })

  it('reports inline marks', () => {
    const view = open('un **gras«»setto** qui')
    expect([...activeMarks(view.state)]).toContain('bold')
  })

  it('tells a task item from a plain bullet', () => {
    expect([...activeMarks(open('- [ ] cosa«»').state)]).toContain('taskList')
    expect([...activeMarks(open('- [ ] cosa«»').state)]).not.toContain('bulletList')
    expect([...activeMarks(open('- cosa«»').state)]).toContain('bulletList')
  })

  it('reports a table and a quote', () => {
    expect([...activeMarks(open('| a«» | b |\n| --- | --- |\n').state)]).toContain('table')
    expect([...activeMarks(open('> citato«»').state)]).toContain('blockquote')
  })
})
