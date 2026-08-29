import { afterEach, describe, expect, it } from 'vitest'
import { EditorSelection, EditorState } from '@codemirror/state'
import { EditorView } from '@codemirror/view'
import { forceParsing } from '@codemirror/language'
import { markdownSourceExtensions } from './extensions'
import {
  annotationLayer,
  locateSourceAnchor,
  paintedAnchors,
  setAnnotations,
  type AnnotationAnchor,
} from './annotationLayer'
import { readSourceAnchor, snapToInlineBoundaries } from './sourceSelection'

// The annotation layer over the source surface. Two properties matter, and
// they are the two the previous architecture could not have by construction:
//
// what is HIGHLIGHTED here is what accept will SPLICE on the server, because
// both sides run the same rule over the same string;
//
// and painting an annotation changes nothing about the document.

const views: EditorView[] = []

function open(doc: string, sel?: { from: number; to: number }): EditorView {
  const view = new EditorView({
    state: EditorState.create({
      doc,
      extensions: [markdownSourceExtensions({ src: doc, onChange: () => {} }), annotationLayer()],
      selection: sel ? EditorSelection.range(sel.from, sel.to) : undefined,
    }),
  })
  forceParsing(view, doc.length, 5_000)
  views.push(view)
  return view
}

function anchor(over: Partial<AnnotationAnchor>): AnnotationAnchor {
  return {
    id: 'a1',
    kind: 'comment',
    status: 'open',
    anchorQuote: null,
    anchorPrefix: null,
    anchorSuffix: null,
    originalText: null,
    proposedText: null,
    ...over,
  }
}

afterEach(() => {
  while (views.length) views.pop()?.destroy()
})

describe('a CRLF body paints where the words are', () => {
  it('converts the search string offset into a document position', () => {
    // `locateSourceAnchor` searches `sliceDoc()`, a STRING. CodeMirror counts
    // a line break as ONE position whatever it is spelled as, so in a CRLF
    // body the two drift by one per preceding line: the highlight used to sit
    // two characters to the right of the words it quoted.
    const doc = 'riga uno\r\nriga due\r\nbersaglio qui\r\n'
    const view = open(doc)
    view.dispatch({ effects: setAnnotations.of([anchor({ anchorQuote: 'bersaglio' })]) })
    const painted = view.contentDOM.querySelector('[data-annotation-id="a1"]')
    expect(painted).not.toBeNull()
    expect(painted!.textContent).toBe('bersaglio')
    expect(view.state.sliceDoc()).toBe(doc)
  })

  it('an LF body is unaffected, which is the identity case', () => {
    const doc = 'riga uno\nriga due\nbersaglio qui\n'
    const view = open(doc)
    view.dispatch({ effects: setAnnotations.of([anchor({ anchorQuote: 'bersaglio' })]) })
    expect(
      view.contentDOM.querySelector('[data-annotation-id="a1"]')!.textContent,
    ).toBe('bersaglio')
  })
})

describe('locating an anchor', () => {
  const doc = 'Il termine **importante** va spiegato.\n'

  it('finds a quote that INCLUDES markup, which the rendered domain could not', () => {
    expect(locateSourceAnchor(doc, anchor({ anchorQuote: '**importante**' }))).toEqual({
      from: 11,
      to: 25,
    })
  })

  it('uses the context to pick between repeats', () => {
    const d = 'primo caso\n\nsecondo caso\n'
    expect(locateSourceAnchor(d, anchor({ anchorQuote: 'caso' }))).toEqual({ from: 6, to: 10 })
    const second = locateSourceAnchor(
      d,
      anchor({ anchorQuote: 'caso', anchorPrefix: 'secondo ', anchorSuffix: '\n' }),
    )
    expect(second?.from).toBeGreaterThan(d.indexOf('secondo'))
  })

  it('skips a range another annotation already took', () => {
    const d = 'caso e caso\n'
    const first = locateSourceAnchor(d, anchor({ anchorQuote: 'caso' }))!
    const second = locateSourceAnchor(d, anchor({ anchorQuote: 'caso' }), [first])
    expect(second).toEqual({ from: 7, to: 11 })
  })

  it('REFUSES a rendered-domain anchor rather than guessing', () => {
    // Its quote is a projection of the document, not a span of it. A miss is
    // a missing highlight; a hit would be a highlight on the wrong passage.
    expect(
      locateSourceAnchor(doc, anchor({ anchorQuote: 'importante', anchorDomain: 'rendered' })),
    ).toBeNull()
    expect(
      locateSourceAnchor(doc, anchor({ anchorQuote: 'importante', anchorDomain: 'source' })),
    ).not.toBeNull()
  })

  it('reads a suggestion from originalText and a comment from anchorQuote', () => {
    expect(
      locateSourceAnchor(doc, anchor({ kind: 'suggestion', originalText: 'termine' })),
    ).not.toBeNull()
    expect(
      locateSourceAnchor(doc, anchor({ kind: 'suggestion', anchorQuote: 'termine' })),
    ).toBeNull()
  })
})

describe('painting', () => {
  const doc = 'Il termine **importante** va spiegato.\n'

  it('marks an open comment and leaves the document alone', () => {
    const view = open(doc)
    view.dispatch({ effects: setAnnotations.of([anchor({ anchorQuote: '**importante**' })]) })
    expect(view.contentDOM.querySelectorAll('.anno-mark--comment')).toHaveLength(1)
    expect(
      view.contentDOM.querySelector('.anno-mark--comment')?.getAttribute('data-annotation-id'),
    ).toBe('a1')
    expect(view.state.sliceDoc()).toBe(doc)
  })

  it('strikes a suggestion and shows the proposal after it', () => {
    const view = open(doc)
    view.dispatch({
      effects: setAnnotations.of([
        anchor({
          kind: 'suggestion',
          originalText: 'importante',
          proposedText: 'essenziale',
        }),
      ]),
    })
    expect(view.contentDOM.querySelectorAll('.anno-mark--del')).toHaveLength(1)
    const ins = view.contentDOM.querySelector('.anno-mark--ins')
    expect(ins?.textContent).toBe('essenziale')
    expect(view.state.sliceDoc()).toBe(doc)
  })

  it('paints nothing for an annotation that is not open', () => {
    const view = open(doc)
    view.dispatch({
      effects: setAnnotations.of([
        anchor({ status: 'resolved', anchorQuote: '**importante**' }),
      ]),
    })
    expect(view.contentDOM.querySelectorAll('.anno-mark')).toHaveLength(0)
  })

  it('orders what it painted by position, for prev/next', () => {
    const d = 'alfa e beta e gamma\n'
    const view = open(d)
    const list = paintedAnchors(view.state, [
      anchor({ id: 'g', anchorQuote: 'gamma' }),
      anchor({ id: 'a', anchorQuote: 'alfa' }),
      anchor({ id: 'b', anchorQuote: 'beta' }),
    ])
    expect(list.map((x) => x.anchor.id)).toEqual(['a', 'b', 'g'])
  })
})

describe('capturing a selection', () => {
  it('quotes the source, markup included', () => {
    const doc = 'Il termine **importante** va spiegato.\n'
    const view = open(doc, { from: 11, to: 25 })
    const sel = readSourceAnchor(view.state)
    expect(sel?.text).toBe('**importante**')
  })

  it('SNAPS out of a delimiter run rather than splitting it', () => {
    // Ending the selection between the two closing asterisks would quote
    // `importante*`, and splicing into that leaves `**essenziale more` --
    // valid markdown that renders as literal asterisks. A human's selection
    // is a gesture, and the gesture meant the whole run.
    const doc = 'Il **importante** qui\n'
    // From the start of the word to BETWEEN the two closing asterisks.
    const from = doc.indexOf('importante')
    const view = open(doc, { from, to: doc.indexOf('**', from) + 1 })
    expect(readSourceAnchor(view.state)?.text).toBe('**importante**')
  })

  it('leaves a selection that covers NEITHER delimiter alone', () => {
    // Selecting just the word inside a bold run is the ordinary way to
    // comment on it. Growing that to the whole run would annotate something
    // the reader did not point at.
    const doc = 'Il **importante** qui\n'
    const from = doc.indexOf('importante')
    const view = open(doc, { from, to: from + 'importante'.length })
    expect(readSourceAnchor(view.state)?.text).toBe('importante')
  })

  it('snaps out of a delimiter the selection STARTS inside', () => {
    const doc = 'Il **importante** qui\n'
    const view = open(doc, { from: 4, to: doc.indexOf('importante') + 4 })
    expect(readSourceAnchor(view.state)?.text).toBe('**importante**')
  })

  it('trims edge whitespace so the quote and the highlight are the same span', () => {
    const doc = 'uno   due   tre\n'
    const view = open(doc, { from: 3, to: 12 })
    const sel = readSourceAnchor(view.state)
    expect(sel?.text).toBe('due')
    expect(view.state.sliceDoc(sel!.from, sel!.to)).toBe('due')
  })

  it('takes each end context from its OWN block', () => {
    // One block for both inverts the bounds the moment a selection spans two
    // of them, which is the ordinary way to quote a pair of paragraphs.
    const doc = 'Primo paragrafo qui.\n\nSecondo paragrafo la.\n'
    const from = doc.indexOf('qui')
    const to = doc.indexOf('paragrafo la')
    const view = open(doc, { from, to })
    const sel = readSourceAnchor(view.state)
    expect(sel).not.toBeNull()
    expect(sel!.prefix.length).toBeGreaterThan(0)
    expect(sel!.suffix.length).toBeGreaterThan(0)
    expect(doc.slice(sel!.from - sel!.prefix.length, sel!.from)).toBe(sel!.prefix)
    expect(doc.slice(sel!.to, sel!.to + sel!.suffix.length)).toBe(sel!.suffix)
  })

  it('is null for an empty or whitespace-only selection', () => {
    const doc = 'testo   qui\n'
    expect(readSourceAnchor(open(doc, { from: 5, to: 5 }).state)).toBeNull()
    expect(readSourceAnchor(open(doc, { from: 5, to: 8 }).state)).toBeNull()
  })
})

describe('snapToInlineBoundaries', () => {
  it('leaves a range that already sits on boundaries alone', () => {
    const doc = 'un **bold** qui\n'
    const view = open(doc)
    expect(snapToInlineBoundaries(view.state, 3, 11)).toEqual({ from: 3, to: 11 })
  })
})
