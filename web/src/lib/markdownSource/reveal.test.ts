import { describe, expect, it } from 'vitest'
import { EditorSelection, EditorState } from '@codemirror/state'
import { overlaps, revealedRanges } from './reveal'

// The scope of the reveal, pinned on its own.
//
// It used to widen to whole lines, and that is what made the surface read as
// neither markdown nor prose: clicking anywhere in a paragraph brought back
// every delimiter on the line at once. The narrowing to the construct is the
// design, so it gets an assertion that does not depend on any layer.

function stateFor(doc: string, anchor: number, head = anchor): EditorState {
  return EditorState.create({ doc, selection: EditorSelection.range(anchor, head) })
}

describe('revealedRanges', () => {
  it('is the selection itself, NOT the line it sits on', () => {
    const doc = 'un **grassetto** qui\n'
    expect(revealedRanges(stateFor(doc, 7))).toEqual([{ from: 7, to: 7 }])
    expect(revealedRanges(stateFor(doc, 3, 16))).toEqual([{ from: 3, to: 16 }])
  })

  it('reports every range of a multi-range selection', () => {
    const state = EditorState.create({
      doc: 'uno\ndue\ntre\n',
      // Without this CodeMirror keeps only the main range.
      extensions: [EditorState.allowMultipleSelections.of(true)],
      selection: EditorSelection.create([
        EditorSelection.range(0, 3),
        EditorSelection.range(8, 11),
      ]),
    })
    expect(revealedRanges(state)).toEqual([
      { from: 0, to: 3 },
      { from: 8, to: 11 },
    ])
  })
})

describe('overlaps', () => {
  it('is inclusive at both ends, which is what reveals a construct from its edge', () => {
    // A caret at either boundary of `**bold**` renders at the same pixel as
    // one just inside it. Both have to bring the delimiters back, or the
    // ambiguity is invisible at exactly the moment it matters.
    const caretAtStart = [{ from: 4, to: 4 }]
    const caretAtEnd = [{ from: 12, to: 12 }]
    expect(overlaps(caretAtStart, 4, 12)).toBe(true)
    expect(overlaps(caretAtEnd, 4, 12)).toBe(true)
    expect(overlaps([{ from: 13, to: 13 }], 4, 12)).toBe(false)
    expect(overlaps([{ from: 3, to: 3 }], 4, 12)).toBe(false)
  })
})
