import { afterEach, describe, expect, it } from 'vitest'
import { EditorState } from '@codemirror/state'
import { EditorView } from '@codemirror/view'
import { forceParsing } from '@codemirror/language'
import { markdownSourceExtensions } from './extensions'
import type { MarkdownMode } from './mode'

// The live-preview layer, asserted on two axes.
//
// First and above all: it is PRESENTATIONAL. Every test here checks that the
// document is untouched, because a decoration layer that rewrote the source
// to display it would have reinvented the serializer this whole substrate
// exists to delete. If only one assertion in this file survives, it should
// be that one.
//
// Second: markup recedes except on the CONSTRUCT the caret is in, so the
// user is always editing real source rather than a rendering of it, without
// the whole line flipping to markup around them.
//
// Third: in SOURCE mode the layer is not installed at all, so there is
// nothing to hide, nothing to replace, and nothing to get wrong.

const views: EditorView[] = []

function open(src: string, mode: MarkdownMode = 'visual'): EditorView {
  const view = new EditorView({
    state: EditorState.create({
      doc: src,
      extensions: markdownSourceExtensions({ src, mode, onChange: () => {} }),
    }),
  })
  // The parser is time-sliced; without this the tree over the whole document
  // may not exist yet and the assertions would race it.
  forceParsing(view, src.length, 5_000)
  views.push(view)
  return view
}

/** What the user sees: the rendered lines, with replaced ranges gone. */
function rendered(view: EditorView): string {
  return Array.from(view.contentDOM.querySelectorAll('.cm-line'))
    .map((el) => el.textContent ?? '')
    .join('\n')
}

function putCaret(view: EditorView, at: number): void {
  view.dispatch({ selection: { anchor: at } })
}

afterEach(() => {
  while (views.length) views.pop()?.destroy()
})

describe('the layer never touches the document', () => {
  const cases: [string, string][] = [
    ['heading', '# Titolo\n\ntesto\n'],
    ['emphasis', 'un **grassetto** e un _corsivo_ e ~~via~~\n'],
    ['inline code', 'usa `pnpm test` per farlo\n'],
    ['link', 'vedi [la guida](https://example.com) e basta\n'],
    ['blockquote', '> citato\n> ancora\n'],
    ['fence', '```js\nlet x = 1\n```\n'],
    ['table', '| a | b |\n| --- | --- |\n| 1 | 2 |\n'],
    ['hard-wrapped prose', 'riga uno,\nriga due.\n'],
  ]

  it.each(cases)('%s: sliceDoc is the source, byte for byte', (_name, src) => {
    const view = open(src)
    expect(view.state.sliceDoc()).toBe(src)
    // And still after the caret moves through it, which is what rebuilds the
    // decorations.
    for (const at of [0, Math.floor(src.length / 2), src.length]) {
      putCaret(view, at)
      expect(view.state.sliceDoc()).toBe(src)
    }
  })

  it('no decoration pass ever emits an onChange', () => {
    const src = '# Titolo\n\nun **grassetto** e [un link](https://example.com)\n'
    const emitted: string[] = []
    const view = new EditorView({
      state: EditorState.create({
        doc: src,
        extensions: markdownSourceExtensions({ src, onChange: (v) => emitted.push(v) }),
      }),
    })
    views.push(view)
    forceParsing(view, src.length, 5_000)
    for (let at = 0; at <= src.length; at += 5) view.dispatch({ selection: { anchor: at } })
    expect(emitted).toEqual([])
  })
})

describe('markup recedes off the construct the caret is in', () => {
  it('hides the heading marker, and gives it back on the line', () => {
    const src = '## Titolo\n\ntesto\n'
    const view = open(src)
    putCaret(view, src.indexOf('testo'))
    expect(rendered(view)).toContain('Titolo')
    expect(rendered(view).split('\n')[0]).not.toContain('#')

    putCaret(view, 3)
    expect(rendered(view).split('\n')[0]).toBe('## Titolo')
  })

  it('hides emphasis delimiters, and gives them back on the line', () => {
    const src = 'un **grassetto** qui\n\naltro\n'
    const view = open(src)
    putCaret(view, src.indexOf('altro'))
    expect(rendered(view).split('\n')[0]).toBe('un grassetto qui')

    putCaret(view, 5)
    expect(rendered(view).split('\n')[0]).toBe('un **grassetto** qui')
  })

  it('shows an inline link as its label, source restored on the line', () => {
    const src = 'vedi [la guida](https://example.com) ok\n\naltro\n'
    const view = open(src)
    putCaret(view, src.indexOf('altro'))
    expect(rendered(view).split('\n')[0]).toBe('vedi la guida ok')

    putCaret(view, 8)
    expect(rendered(view).split('\n')[0]).toBe('vedi [la guida](https://example.com) ok')
  })

  it('hides inline-code backticks but NOT a fence', () => {
    const src = 'usa `pnpm test`\n\n```js\nlet x = 1\n```\n'
    const view = open(src)
    putCaret(view, src.length)
    const lines = rendered(view).split('\n')
    expect(lines[0]).toBe('usa pnpm test')
    // A fence marker is also a CodeMark. Hiding it would delete the only
    // thing telling a reader where the code block starts and ends.
    expect(lines[2]).toBe('```js')
    expect(lines[4]).toBe('```')
  })

  it('leaves an autolink alone', () => {
    // An autolink is all URL: hiding the destination leaves an empty line,
    // and there is no label to put in its place. (An image used to be in
    // this test for a related reason -- nothing to show instead of the
    // source. It has a widget now; see images.test.ts.)
    const src = '<https://example.com>\n\naltro\n'
    const view = open(src)
    putCaret(view, src.length)
    expect(rendered(view).split('\n')[0]).toBe('<https://example.com>')
    expect(view.state.sliceDoc()).toBe(src)
  })

  it('eats the separator after a block delimiter but not around an inline one', () => {
    const src = '> citato\n> > annidato\n\nun **bold** e `code` qui\n'
    const view = open(src)
    putCaret(view, src.length)
    const lines = rendered(view).split('\n')
    // No leading space left behind by the hidden `>`.
    expect(lines[0]).toBe('citato')
    expect(lines[1]).toBe('annidato')
    // The author's spaces around inline markup survive.
    expect(lines[3]).toBe('un bold e code qui')
    expect(view.state.sliceDoc()).toBe(src)
  })

  it('reveals ONE construct, leaving the rest of the line rendered', () => {
    // This is the narrowing that makes the rendered view a rendered view.
    // Revealing the whole line brought back every `**`, every `[` and every
    // `](url)` on it at once, and the text jumped: the surface read as
    // neither markdown nor prose.
    const src = 'un **bold** e [un link](https://example.com) qui\n'
    const view = open(src)
    putCaret(view, src.indexOf('bold') + 1)
    expect(rendered(view).split('\n')[0]).toBe('un **bold** e un link qui')
    putCaret(view, src.indexOf('un link') + 2)
    expect(rendered(view).split('\n')[0]).toBe('un bold e [un link](https://example.com) qui')
  })

  it('a caret at either EDGE of a construct reveals it', () => {
    // The edges are where the position is ambiguous -- two document offsets
    // render at the same pixel when the delimiter between them is hidden --
    // so they are exactly where the delimiters have to be visible.
    const src = 'un **bold** qui\n'
    const view = open(src)
    for (const at of [src.indexOf('bold'), src.indexOf('bold') + 4]) {
      putCaret(view, at)
      expect(rendered(view).split('\n')[0]).toBe('un **bold** qui')
    }
  })

  it('nested constructs sharing a delimiter run reveal together', () => {
    // `***molto***` is an Emphasis wrapping a StrongEmphasis, spelling ONE
    // run of three asterisks. Revealing the outer alone put a single `*` on
    // each side, which is markup for neither construct.
    const src = 'un ***molto*** forte\n'
    const view = open(src)
    for (const at of [3, 5, 8, 12, 14]) {
      putCaret(view, at)
      expect(rendered(view).split('\n')[0]).toBe('un ***molto*** forte')
    }
    putCaret(view, src.indexOf('forte'))
    expect(rendered(view).split('\n')[0]).toBe('un molto forte')
  })

  it('a caret inside two nested constructs reveals both, and nothing else', () => {
    // It is inside the link AND inside the bold run, so both show their
    // markup. What must NOT happen is the rule spilling further: the second
    // bold run on the same line stays rendered, which is what would break if
    // the owner climbed to the paragraph.
    const src = 'un **grasso [una guida](https://esempio.it)** e **altro** qui\n'
    const view = open(src)
    putCaret(view, src.indexOf('una guida') + 2)
    expect(rendered(view).split('\n')[0]).toBe(
      'un **grasso [una guida](https://esempio.it)** e altro qui',
    )
  })

  it('an unbalanced opener is never hidden, because there is no node to hide', () => {
    // BALANCE, not completeness, is what triggers hiding. Load-bearing and
    // not obvious: `**bol` is safe, but `**bold*` one keystroke later is
    // balanced AS ITALIC and would be hidden if the caret were not on it.
    for (const half of ['**bol', '*ital', '`co', '~~s']) {
      const src = `${half}\n\naltro\n`
      const view = open(src)
      putCaret(view, src.indexOf('altro'))
      expect(rendered(view).split('\n')[0]).toBe(half)
    }
  })

  it('the transient parse while `**bold**` is typed stays visible', () => {
    // `x **bold*` really is Emphasis[3,9] at that instant. The caret is
    // there, mid-word, so the construct is revealed and nothing shifts under
    // the person typing.
    const src = 'x **bold*'
    const view = open(src)
    putCaret(view, src.length)
    expect(rendered(view).split('\n')[0]).toBe(src)
  })

  it('a selection spanning several lines reveals all of them', () => {
    const src = '# Uno\n\n## Due\n\n### Tre\n'
    const view = open(src)
    view.dispatch({ selection: { anchor: 0, head: src.indexOf('Due') + 3 } })
    const lines = rendered(view).split('\n')
    expect(lines[0]).toBe('# Uno')
    expect(lines[2]).toBe('## Due')
    // Outside the selection, markup still recedes.
    expect(lines[4]).toBe('Tre')
  })
})

describe('the editor does not draw what the reader will not render', () => {
  // lezer makes a `Link` node out of any bracket run. Hiding those brackets
  // drew `array[0]` as `array0` and `nota[^1]` as `nota^1` -- neither the
  // source nor the rendering, and the `0` became indistinguishable from part
  // of the word.
  const literal: [string, string][] = [
    ['a shortcut bracket run', 'see [proven] status'],
    ['an index expression', 'vedi array[0] qui'],
    ['a full reference link', 'vedi [a][ref] qui'],
    ['a collapsed reference link', 'vedi [b][] qui'],
    ['a footnote reference', 'testo con nota[^1]'],
  ]

  it.each(literal)('%s stays its own source', (_name, first) => {
    const src = `${first}\n\naltro\n`
    const view = open(src)
    putCaret(view, src.indexOf('altro'))
    expect(rendered(view).split('\n')[0]).toBe(first)
    expect(view.state.sliceDoc()).toBe(src)
  })

  it('an INLINE link is still shown as its label', () => {
    const src = 'vedi [la guida](https://example.com) ok\n\naltro\n'
    const view = open(src)
    putCaret(view, src.indexOf('altro'))
    expect(rendered(view).split('\n')[0]).toBe('vedi la guida ok')
  })

})

describe('source mode installs no preview at all', () => {
  const cases = [
    '# Titolo\n\nun **grassetto** e [un link](https://example.com)\n',
    '| a | b |\n| --- | --- |\n| 1 | 2 |\n',
    '```mermaid\ngraph TD; A-->B;\n```\n',
  ]

  it.each(cases.map((s) => [s.split('\n')[0], s]))(
    '%s: every line reads as its own source',
    (_name, src) => {
      const view = open(src, 'source')
      putCaret(view, 0)
      // The trailing newline is the empty last line, so the join is the
      // source byte for byte.
      expect(rendered(view)).toBe(src)
      expect(view.state.sliceDoc()).toBe(src)
      expect(view.contentDOM.querySelectorAll('[class*="cm-md-"]')).toHaveLength(0)
    },
  )
})

describe('structure gets a line class instead of a rewrite', () => {
  it('marks headings, quotes, code and rules', () => {
    const src = '# H\n\n> q\n\n```\nc\n```\n\n---\n'
    const view = open(src)
    putCaret(view, 0)
    const classes = Array.from(view.contentDOM.querySelectorAll('.cm-line')).map(
      (el) => el.className,
    )
    expect(classes.some((c) => c.includes('cm-md-h1'))).toBe(true)
    expect(classes.some((c) => c.includes('cm-md-quote'))).toBe(true)
    expect(classes.some((c) => c.includes('cm-md-code'))).toBe(true)
    expect(classes.some((c) => c.includes('cm-md-hr'))).toBe(true)
    expect(view.state.sliceDoc()).toBe(src)
  })
})
