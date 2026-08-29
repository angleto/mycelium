import { afterEach, describe, expect, it } from 'vitest'
import { EditorState } from '@codemirror/state'
import { EditorView } from '@codemirror/view'
import { undo } from '@codemirror/commands'
import { markdownSourceExtensions } from './extensions'
import { syncExternal } from './syncExternal'

// Installing a body the user did not type: a conflict reload, an accepted
// suggestion, a different note part. It is the one write path with no
// keystroke behind it, so a mistake here corrupts a document nobody was
// editing.

const views: EditorView[] = []

function open(src: string): EditorView {
  const view = new EditorView({
    state: EditorState.create({
      doc: src,
      extensions: markdownSourceExtensions({ src, onChange: () => {} }),
    }),
  })
  views.push(view)
  return view
}

const extensionsFor = (src: string) => markdownSourceExtensions({ src, onChange: () => {} })

function sync(view: EditorView, next: string): string[] {
  const rebuilt: string[] = []
  syncExternal(view, next, extensionsFor, (t) => rebuilt.push(t))
  return rebuilt
}

afterEach(() => {
  while (views.length) views.pop()?.destroy()
})

describe('the smallest change that produces the new body', () => {
  it('installs it exactly, LF', () => {
    const view = open('riga uno\nriga due\nbersaglio qui\n')
    const next = 'riga uno\nriga due\nBERSAGLIO qui\n'
    expect(sync(view, next)).toEqual([])
    expect(view.state.sliceDoc()).toBe(next)
  })

  it('installs it exactly, CRLF', () => {
    // The diff is computed over the slice STRING, whose offsets drift from
    // document positions by one per preceding line break in a CRLF body.
    // Dispatching the raw offsets spliced the change that many columns left
    // and corrupted the body it was supposed to install.
    const view = open('riga uno\r\nriga due\r\nbersaglio qui\r\n')
    const next = 'riga uno\r\nriga due\r\nBERSAGLIO qui\r\n'
    expect(sync(view, next)).toEqual([])
    expect(view.state.sliceDoc()).toBe(next)
  })

  it('installs a change at the very end of a long CRLF body', () => {
    const lines = Array.from({ length: 40 }, (_, i) => `riga ${i}`)
    const src = lines.join('\r\n') + '\r\n'
    const view = open(src)
    const next = src + 'coda\r\n'
    expect(sync(view, next)).toEqual([])
    expect(view.state.sliceDoc()).toBe(next)
  })

  it('installs a change at the very start of a CRLF body', () => {
    const view = open('uno\r\ndue\r\ntre\r\n')
    const next = 'UNO\r\ndue\r\ntre\r\n'
    expect(sync(view, next)).toEqual([])
    expect(view.state.sliceDoc()).toBe(next)
  })

  it('a value equal to the document does nothing at all', () => {
    const src = 'uguale\r\n'
    const view = open(src)
    expect(sync(view, src)).toEqual([])
    expect(view.state.sliceDoc()).toBe(src)
  })

  it('does not enter the undo stack', () => {
    // Accepting a suggestion reloads the body from the server. If that were
    // undoable, one Cmd+Z would write the pre-accept body back over the
    // accepted one while the annotation stayed `accepted`.
    const view = open('prima\n')
    sync(view, 'sostituito\n')
    undo(view)
    expect(view.state.sliceDoc()).toBe('sostituito\n')
  })
})

describe('a rebuild that changes the bytes reports itself', () => {
  it('a body switching separator is installed exactly, and reports nothing', () => {
    const view = open('uno\ndue\n')
    const next = 'uno\r\ndue\r\n'
    expect(sync(view, next)).toEqual([])
    expect(view.state.sliceDoc()).toBe(next)
  })

  it('a MIXED body normalises, and says so', () => {
    // A rebuild is not a document change, so the update listener stays quiet.
    // Without this report the host would keep bytes the editor has already
    // discarded, and the next single keystroke would emit the whole rewritten
    // body and autosave it as a one-character edit.
    const view = open('uno\r\ndue\r\n')
    const rebuilt = sync(view, 'uno\r\ndue\ntre\r\n')
    expect(rebuilt).toEqual(['uno\ndue\ntre\n'])
    expect(view.state.sliceDoc()).toBe('uno\ndue\ntre\n')
  })
})
