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

  it('on an empty selection leaves the caret between the delimiters', () => {
    const view = open('scrivi «» qui')
    toggleWrap('bold')(view)
    expect(view.state.sliceDoc()).toBe('scrivi **** qui')
    expect(view.state.selection.main.head).toBe(9)
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

  it('nothing table-shaped fires outside a table', () => {
    expect(run('solo testo«»', addRowAfter).ok).toBe(false)
    expect(run('solo testo«»', nextCell).ok).toBe(false)
    expect(run('solo testo«»', deleteTable).ok).toBe(false)
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
