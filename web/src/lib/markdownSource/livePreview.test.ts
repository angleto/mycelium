import { afterEach, describe, expect, it } from 'vitest'
import { EditorState } from '@codemirror/state'
import { EditorView } from '@codemirror/view'
import { forceParsing } from '@codemirror/language'
import { markdownSourceExtensions } from './extensions'

// The live-preview layer, asserted on two axes.
//
// First and above all: it is PRESENTATIONAL. Every test here checks that the
// document is untouched, because a decoration layer that rewrote the source
// to display it would have reinvented the serializer this whole substrate
// exists to delete. If only one assertion in this file survives, it should
// be that one.
//
// Second: markup recedes off the caret's line and comes back on it, so the
// user is always editing real source rather than a rendering of it.

const views: EditorView[] = []

function open(src: string): EditorView {
  const view = new EditorView({
    state: EditorState.create({
      doc: src,
      extensions: markdownSourceExtensions({ src, onChange: () => {} }),
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

describe('markup recedes off the caret line', () => {
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

  it('leaves an autolink and an image alone', () => {
    // An autolink is all URL: hiding it leaves an empty line. An image has
    // no widget to stand in for it yet, so hiding its source would make it
    // invisible rather than rendered.
    const src = '<https://example.com>\n\n![alt](/attachments/x/download)\n'
    const view = open(src)
    putCaret(view, src.length)
    const lines = rendered(view).split('\n')
    expect(lines[0]).toBe('<https://example.com>')
    expect(lines[2]).toBe('![alt](/attachments/x/download)')
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
