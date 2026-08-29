import { afterEach, describe, expect, it, vi } from 'vitest'
import { EditorSelection, EditorState } from '@codemirror/state'
import { EditorView } from '@codemirror/view'
import { forceParsing } from '@codemirror/language'
import { undo } from '@codemirror/commands'
import { markdownSourceExtensions } from './extensions'
import { setMarkdownMode, type MarkdownMode } from './mode'
import { setAnnotations, type AnnotationAnchor } from './annotationLayer'

// The toggle between the two views, asserted where it matters: on the
// DOCUMENT.
//
// The retired pair of surfaces were two document models, and the visual one
// could only save what its serializer produced -- so opening a body in it was
// safe and editing it once was not. These two are two RENDERINGS of one
// document. Every assertion here is a restatement of that: the bytes, the
// history, the selection and everything layered on top survive a switch,
// because a switch reconfigures decorations and touches nothing else.

const views: EditorView[] = []

function open(src: string, mode: MarkdownMode = 'visual'): { view: EditorView; emitted: string[] } {
  const emitted: string[] = []
  const view = new EditorView({
    state: EditorState.create({
      doc: src,
      extensions: markdownSourceExtensions({ src, mode, onChange: (v) => emitted.push(v) }),
    }),
    // Attached, so the theme's stylesheet is in the document and the
    // typography assertions below have a cascade to resolve against.
    parent: document.body,
  })
  forceParsing(view, src.length, 5_000)
  views.push(view)
  return { view, emitted }
}

const RICH = [
  '# Titolo',
  '',
  'un **grassetto**, un `codice` e [una guida](https://example.com).',
  '',
  '| a | b |',
  '| --- | --- |',
  '| 1 | 2 |',
  '',
  '```mermaid',
  'graph TD; A-->B;',
  '```',
  '',
].join('\n')

function comment(id: string, quote: string): AnnotationAnchor {
  return {
    id,
    kind: 'comment',
    status: 'open',
    anchorQuote: quote,
    anchorPrefix: null,
    anchorSuffix: null,
    originalText: null,
    proposedText: null,
    anchorDomain: 'source',
  }
}

afterEach(() => {
  while (views.length) views.pop()?.destroy()
})

describe('what each mode shows', () => {
  it('source mode replaces nothing and classes nothing', () => {
    const { view } = open(RICH, 'source')
    const text = Array.from(view.contentDOM.querySelectorAll('.cm-line'))
      .map((el) => el.textContent ?? '')
      .join('\n')
    expect(text).toBe(RICH)
    expect(view.contentDOM.querySelectorAll('[class*="cm-md-"]')).toHaveLength(0)
  })

  it('visual mode draws the blocks and hides the markup', () => {
    const { view } = open(RICH, 'visual')
    view.dispatch({ selection: { anchor: 0 } })
    expect(view.contentDOM.querySelector('.cm-md-table')).not.toBeNull()
    expect(view.contentDOM.querySelector('.cm-md-mermaid')).not.toBeNull()
    const text = view.contentDOM.textContent ?? ''
    expect(text).toContain('un grassetto, un codice e una guida.')
  })
})

describe('the typography is the visible difference', () => {
  // CodeMirror ships its own `.cm-scroller { font-family: monospace }` in a
  // StyleModule sheet, and a rule in index.css could not reliably beat it
  // (the load order is not guaranteed). These assert that the mode themes do.
  const fontOf = (view: EditorView) =>
    getComputedStyle(view.dom.querySelector('.cm-scroller') as HTMLElement).fontFamily

  it('markdown mode is monospace, the rendered view is the body face', () => {
    expect(fontOf(open(RICH, 'source').view)).toBe('var(--mono)')
    expect(fontOf(open(RICH, 'visual').view)).toBe('var(--sans)')
  })

  it('a rendered heading gets the reader\'s own heading rule', () => {
    // Serif display face, 600, --text-h: the same three values index.css
    // gives every h1-h3, per the brand guidelines. The syntax highlighter
    // would otherwise override all three, since it marks the text INSIDE the
    // heading and a mark beats a line class.
    const { view } = open(RICH, 'visual')
    view.dispatch({ selection: { anchor: RICH.indexOf('grassetto') } })
    const h1 = view.contentDOM.querySelector('.cm-md-h1') as HTMLElement
    expect(h1).not.toBeNull()
    const style = getComputedStyle(h1)
    expect(style.fontFamily).toBe('var(--font-display)')
    expect(style.fontWeight).toBe('600')
    expect(style.color).toBe('var(--text-h)')
  })
})

describe('switching modes is not an edit', () => {
  it('emits nothing and changes no byte, in either direction', () => {
    const { view, emitted } = open(RICH, 'visual')
    setMarkdownMode(view, 'source')
    expect(view.state.sliceDoc()).toBe(RICH)
    expect(emitted).toEqual([])
    setMarkdownMode(view, 'visual')
    expect(view.state.sliceDoc()).toBe(RICH)
    expect(emitted).toEqual([])
  })

  it('keeps the undo history', () => {
    // A `view.setState` would have dropped it. This is a compartment
    // reconfigure, so the history field is the same field.
    const { view } = open('testo\n', 'visual')
    view.dispatch({ changes: { from: 0, insert: 'X' }, userEvent: 'input.type' })
    expect(view.state.sliceDoc()).toBe('Xtesto\n')
    setMarkdownMode(view, 'source')
    undo(view)
    expect(view.state.sliceDoc()).toBe('testo\n')
  })

  it('keeps the selection', () => {
    const { view } = open(RICH, 'visual')
    const range = EditorSelection.range(4, 10)
    view.dispatch({ selection: range })
    setMarkdownMode(view, 'source')
    expect(view.state.selection.main.from).toBe(4)
    expect(view.state.selection.main.to).toBe(10)
  })

  it('re-asserts the selection so the annotation surface re-measures', () => {
    // A bare reconfigure carries neither docChanged nor selectionSet, so
    // nothing would repaint or notify -- and the popover would go on using
    // coordinates measured under the typography that has just been replaced.
    const seen: boolean[] = []
    const src = 'testo\n'
    const view = new EditorView({
      state: EditorState.create({
        doc: src,
        extensions: [
          ...markdownSourceExtensions({ src, onChange: () => {} }),
          EditorView.updateListener.of((u) => {
            if (u.selectionSet) seen.push(true)
          }),
        ],
      }),
    })
    views.push(view)
    setMarkdownMode(view, 'source')
    expect(seen.length).toBeGreaterThan(0)
  })

  it('keeps the annotation marks: the layer is outside the compartment', () => {
    const src = 'un **titolo** lungo\n'
    const { view } = open(src, 'visual')
    view.dispatch({
      effects: setAnnotations.of([comment('a1', 'titolo')]),
    })
    expect(view.contentDOM.querySelectorAll('[data-annotation-id="a1"]').length).toBe(1)
    setMarkdownMode(view, 'source')
    expect(view.contentDOM.querySelectorAll('[data-annotation-id="a1"]').length).toBe(1)
  })

  it('an annotation spanning hidden markup is ONE span, not three', () => {
    // Decoration rank is extension order. If the presentation compartment
    // outranked the annotation layer, the mark would be closed before each
    // hidden `**` and reopened after it: one comment painted as three
    // fragments, with the highlight broken at every delimiter.
    const src = 'prima un **titolo** lungo dopo\n'
    const { view } = open(src, 'visual')
    view.dispatch({ selection: { anchor: 0 } })
    view.dispatch({
      effects: setAnnotations.of([comment('a2', 'un **titolo** lungo')]),
    })
    expect(view.contentDOM.querySelectorAll('[data-annotation-id="a2"]').length).toBe(1)
  })

  it('keeps the entity chips: they are outside the compartment too', async () => {
    const src = 'vedi `91cf6aaa` per il resto\n'
    const { view } = open(src, 'visual')
    await vi.waitFor(() =>
      expect(view.contentDOM.querySelectorAll('[data-entity-prefix]').length).toBe(1),
    )
    setMarkdownMode(view, 'source')
    expect(view.contentDOM.querySelectorAll('[data-entity-prefix]').length).toBe(1)
  })
})
