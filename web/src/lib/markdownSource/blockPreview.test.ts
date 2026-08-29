import { afterEach, describe, expect, it } from 'vitest'
import { EditorState } from '@codemirror/state'
import { EditorView } from '@codemirror/view'
import { forceParsing } from '@codemirror/language'
import { markdownSourceExtensions } from './extensions'
import { rowAlignments, splitRow } from './widgets'

// The block preview layer. As with the inline one, the first assertion in
// every test is that the DOCUMENT is untouched: these decorations replace
// whole ranges of lines with widgets, which is the most invasive thing this
// editor does to what you see, and the least it is allowed to do to what you
// have written.

const views: EditorView[] = []

function open(src: string): EditorView {
  const view = new EditorView({
    state: EditorState.create({
      doc: src,
      extensions: markdownSourceExtensions({ src, onChange: () => {} }),
    }),
  })
  forceParsing(view, src.length, 5_000)
  views.push(view)
  return view
}

function lines(view: EditorView): string[] {
  return Array.from(view.contentDOM.querySelectorAll('.cm-line')).map(
    (el) => el.textContent ?? '',
  )
}

function putCaret(view: EditorView, at: number): void {
  view.dispatch({ selection: { anchor: at } })
}

afterEach(() => {
  while (views.length) views.pop()?.destroy()
})

describe('the block layer never touches the document', () => {
  const cases: [string, string][] = [
    ['mermaid', '```mermaid\ngraph TD\n  A-->B\n```\n'],
    ['math block', '$$\n\\sum_{i=0}^{n} x_i\n$$\n'],
    ['single-line math', '$$ x^2 $$\n'],
    ['table', '| a | b |\n| :-- | --: |\n| 1 | 2 |\n'],
    ['setext', 'Titolo\n======\n\ntesto\n'],
    ['plain fence', '```js\nlet x = 1\n```\n'],
  ]

  it.each(cases)('%s', (_name, src) => {
    const view = open(src)
    expect(view.state.sliceDoc()).toBe(src)
    for (const at of [0, Math.floor(src.length / 2), src.length]) {
      putCaret(view, at)
      expect(view.state.sliceDoc()).toBe(src)
    }
  })
})

describe('clicking a widget gives its source back', () => {
  // The route that keeps a rendered block EDITABLE. A widget is
  // contentEditable=false, so without it there is no way to put the caret
  // inside a table -- and with no caret there, `tableAt` never matches, so
  // Tab, +row, +col, delete-table and re-align all stop existing.
  const cases: [string, string, string][] = [
    ['table', 'prima\n\n| a | b |\n| :-- | --: |\n| 1 | 2 |\n\ndopo\n', '.cm-md-table'],
    ['mermaid', 'prima\n\n```mermaid\ngraph TD\n  A-->B\n```\n\ndopo\n', '.cm-md-mermaid'],
    ['math', 'prima\n\n$$\nx^2 + y^2\n$$\n\ndopo\n', '.cm-md-math'],
  ]

  it.each(cases)('%s', (_name, src, selector) => {
    const emitted: string[] = []
    const view = new EditorView({
      state: EditorState.create({
        doc: src,
        extensions: markdownSourceExtensions({ src, onChange: (v) => emitted.push(v) }),
      }),
    })
    views.push(view)
    forceParsing(view, src.length, 5_000)
    putCaret(view, 0)
    const widget = view.contentDOM.querySelector(selector) as HTMLElement | null
    expect(widget).not.toBeNull()
    // A real, RESOLVED hint rather than a bare cursor change: an unresolved
    // key comes back as the key itself, which is truthy and would pass a
    // weaker assertion.
    expect(widget!.title).toBeTruthy()
    expect(widget!.title).not.toBe('editor.revealBlock')

    widget!.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }))
    // The source is back, and not one byte moved to get it there.
    expect(lines(view).join('\n')).toContain(src.split('\n')[3])
    expect(view.state.sliceDoc()).toBe(src)
    expect(emitted).toEqual([])
  })
})

describe('a secondary click leaves a widget alone', () => {
  it('right-clicking a rendered table neither moves the caret nor eats the menu', () => {
    // The handler calls preventDefault, so firing it on a secondary button
    // would suppress the context menu and do nothing but move the caret:
    // right-clicking a diagram to copy it would be a dead gesture.
    const src = 'prima\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n\ndopo\n'
    const view = open(src)
    putCaret(view, 0)
    const widget = view.contentDOM.querySelector('.cm-md-table') as HTMLElement
    const event = new MouseEvent('mousedown', { bubbles: true, cancelable: true, button: 2 })
    widget.dispatchEvent(event)
    expect(event.defaultPrevented).toBe(false)
    expect(view.state.selection.main.head).toBe(0)
    expect(view.contentDOM.querySelector('.cm-md-table')).not.toBeNull()
  })
})

describe('front matter is markdown like everything else', () => {
  it('renders as the reader renders it: a rule, then a setext heading', () => {
    // Deliberately NOT special-cased. To CommonMark the opening `---` is a
    // thematic break, `title: x` followed by `---` is a setext h2, and that is
    // exactly what the read-side renderer draws too (Markdown.tsx runs no
    // front-matter plugin). An editor that drew it as a dimmed metadata block
    // would be showing something the reader never shows, which is the failure
    // the preview layers exist to avoid -- and telling front matter apart from
    // a document that merely opens with a thematic break is not decidable
    // from the text. Stripping it is a change to BOTH surfaces, not to this
    // one. See docs/markdown-syntax.md.
    const src = '---\ntitle: x\ntags: [a, b]\n---\n\ncorpo\n'
    const view = open(src)
    putCaret(view, src.indexOf('corpo'))
    // The `---` is folded into the heading above it, as for any setext
    // heading, and one click on that line brings it straight back.
    expect(lines(view).join('\n')).not.toContain('title: x\n---')
    expect(view.state.sliceDoc()).toBe(src)
    putCaret(view, src.indexOf('title'))
    expect(lines(view).join('\n')).toContain('---')
    expect(view.state.sliceDoc()).toBe(src)
  })
})

describe('setext folding', () => {
  it('folds the underline into the heading, off the caret line', () => {
    const src = 'Titolo\n======\n\ntesto\n'
    const view = open(src)
    putCaret(view, src.indexOf('testo'))
    // Four source lines render as three: the underline is folded into the
    // heading rather than left behind as a blank.
    expect(lines(view)).toEqual(['Titolo', '', 'testo', ''])
    expect(lines(view).join('\n')).not.toContain('======')
    expect(view.state.sliceDoc()).toBe(src)
  })

  it('gives the underline back when the caret is on it', () => {
    const src = 'Titolo\n======\n\ntesto\n'
    const view = open(src)
    putCaret(view, 8)
    expect(lines(view)).toEqual(['Titolo', '======', '', 'testo', ''])
    expect(view.state.sliceDoc()).toBe(src)
  })
})

describe('block widgets', () => {
  it('replaces a mermaid fence with the diagram, and keeps it while editing', () => {
    const src = 'prima\n\n```mermaid\ngraph TD\n  A-->B\n```\n\ndopo\n'
    const view = open(src)
    putCaret(view, 0)
    expect(view.contentDOM.querySelectorAll('.cm-md-mermaid')).toHaveLength(1)
    // Off the caret, the source is replaced by the widget.
    expect(lines(view).join('\n')).not.toContain('graph TD')

    // On it, the source is back AND the diagram stays: writing a diagram you
    // cannot see while you type it is not writing it.
    putCaret(view, src.indexOf('graph TD'))
    expect(lines(view).join('\n')).toContain('graph TD')
    expect(view.contentDOM.querySelectorAll('.cm-md-mermaid')).toHaveLength(1)
    expect(view.state.sliceDoc()).toBe(src)
  })

  it('leaves a non-mermaid fence as source', () => {
    const src = '```js\nlet x = 1\n```\n'
    const view = open(src)
    putCaret(view, src.length)
    expect(lines(view).join('\n')).toContain('let x = 1')
    expect(view.contentDOM.querySelectorAll('.cm-md-widget')).toHaveLength(0)
  })

  it('typesets a $$ block, and keeps the formula while editing it', () => {
    const src = 'prima\n\n$$\nx^2 + y^2\n$$\n\ndopo\n'
    const view = open(src)
    putCaret(view, 0)
    const math = view.contentDOM.querySelectorAll('.cm-md-math')
    expect(math).toHaveLength(1)
    // KaTeX ran: it emits a .katex element, not the raw tex.
    expect(math[0].querySelector('.katex')).not.toBeNull()
    expect(lines(view).join('\n')).not.toContain('x^2 + y^2')

    putCaret(view, src.indexOf('x^2'))
    expect(lines(view).join('\n')).toContain('x^2 + y^2')
    expect(view.contentDOM.querySelectorAll('.cm-md-math')).toHaveLength(1)
    expect(view.state.sliceDoc()).toBe(src)
  })

  it('renders a table, with its alignments, and gives the source back on click-in', () => {
    const src = 'prima\n\n| a | b |\n| :-- | --: |\n| 1 | 2 |\n\ndopo\n'
    const view = open(src)
    putCaret(view, 0)
    const table = view.contentDOM.querySelector('.cm-md-table table')
    expect(table).not.toBeNull()
    const ths = table!.querySelectorAll('th')
    expect(Array.from(ths).map((el) => el.textContent)).toEqual(['a', 'b'])
    expect((ths[0] as HTMLElement).style.textAlign).toBe('left')
    expect((ths[1] as HTMLElement).style.textAlign).toBe('right')
    expect(Array.from(table!.querySelectorAll('td')).map((el) => el.textContent)).toEqual([
      '1',
      '2',
    ])

    putCaret(view, src.indexOf('| 1 | 2 |'))
    expect(view.contentDOM.querySelector('.cm-md-table')).toBeNull()
    expect(lines(view).join('\n')).toContain('| :-- | --: |')
    expect(view.state.sliceDoc()).toBe(src)
  })
})

describe('table cell splitting', () => {
  it('treats an escaped pipe as content, not a separator', () => {
    // The tiptap serializer unescaped this, the row gained a cell, and the
    // last one was destroyed. Here it is a cell containing a pipe.
    expect(splitRow('| a \\| b | c |')).toEqual(['a | b', 'c'])
  })

  it('handles optional leading and trailing pipes', () => {
    expect(splitRow('| a | b |')).toEqual(['a', 'b'])
    expect(splitRow('a | b')).toEqual(['a', 'b'])
    expect(splitRow('| a | b')).toEqual(['a', 'b'])
  })

  it('reads every GFM alignment', () => {
    expect(rowAlignments('| :-- | --: | :-: | --- |')).toEqual([
      'left',
      'right',
      'center',
      null,
    ])
  })
})
