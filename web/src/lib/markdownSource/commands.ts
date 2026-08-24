import { syntaxTree } from '@codemirror/language'
import type { ChangeSpec, EditorState, SelectionRange } from '@codemirror/state'
import { EditorSelection } from '@codemirror/state'
import type { EditorView } from '@codemirror/view'
import { redo, undo } from '@codemirror/commands'
import { mdLink } from '../markdownInline'

// The toolbar, as transformations of markdown SOURCE.
//
// In the document-model surface this replaced, a toolbar button was an
// operation on the model and the serializer decided what characters came
// out. Here there is no serializer, so a button IS the characters: `bold`
// inserts `**`. That makes
// every command a total function `(string, selection) -> string`, which is
// why the tests below assert exact bytes rather than a rendered result.
//
// It also introduces a failure mode the model-based version could not have:
// string surgery across a construct it does not understand. Wrapping a
// selection that starts in a paragraph and ends inside a fenced code block
// in `**` produces markdown that is locally broken. So every command that
// touches inline markup refuses when its range crosses a block boundary or
// lands somewhere the characters would not mean what they say. The class is
// smaller than the one it replaces, and unlike it, enumerable.

/** Names the toolbar uses for its pressed state. */
export type ActiveMark =
  | 'bold'
  | 'italic'
  | 'strike'
  | 'code'
  | 'codeBlock'
  | 'bulletList'
  | 'orderedList'
  | 'taskList'
  | 'blockquote'
  | 'link'
  | 'table'
  | 'heading1'
  | 'heading2'
  | 'heading3'

// Somewhere inline markup would be literal text rather than markup. A `**`
// typed into a code fence is two asterisks, and a `|` typed into a table
// delimiter row changes the table's shape.
const OPAQUE = new Set([
  'CodeText',
  'CodeInfo',
  'CodeMark',
  'FencedCode',
  'CodeBlock',
  'InlineCode',
  'TableDelimiter',
  'Comment',
  'CommentBlock',
])

function inOpaque(state: EditorState, pos: number): boolean {
  for (let n = syntaxTree(state).resolveInner(pos, -1); n; n = n.parent as never) {
    if (OPAQUE.has(n.name)) return true
    if (!n.parent) return false
  }
  return false
}

/**
 * May an inline command touch this range?
 *
 * Two refusals. A range crossing a BLANK LINE spans two blocks, and one pair
 * of delimiters cannot wrap two paragraphs -- the opener would close against
 * text in the next one. And a range whose ends sit inside code or a table's
 * delimiter row would gain characters that are not markup there.
 */
export function canEditInline(state: EditorState, range: SelectionRange): boolean {
  if (inOpaque(state, range.from) || inOpaque(state, range.to)) return false
  if (range.empty) return true
  return !/\n[ \t]*\n/.test(state.sliceDoc(range.from, range.to))
}

// --- inline wrapping --------------------------------------------------------

/** Delimiters a toggle recognises when REMOVING, in preference order. The
 *  first is what it writes when adding. */
const WRAPS: Record<'bold' | 'italic' | 'strike' | 'code', string[]> = {
  // `**` for bold and `_` for italic: `*` for italic next to `**` for bold
  // produces `***text***`, whose parse depends on the delimiter run and
  // which no author reads confidently. Both spellings are RECOGNISED when
  // un-toggling, because content written by the previous editor used `*`.
  bold: ['**', '__'],
  italic: ['_', '*'],
  strike: ['~~'],
  code: ['`'],
}

function wrappedWith(state: EditorState, range: SelectionRange, d: string): boolean {
  const inner = state.sliceDoc(range.from, range.to)
  if (inner.startsWith(d) && inner.endsWith(d) && inner.length >= 2 * d.length) return true
  const before = state.sliceDoc(Math.max(0, range.from - d.length), range.from)
  const after = state.sliceDoc(range.to, Math.min(state.doc.length, range.to + d.length))
  return before === d && after === d
}

/** Toggle an inline wrap over every selection range. */
export function toggleWrap(kind: keyof typeof WRAPS) {
  return (view: EditorView): boolean => {
    const { state } = view
    if (state.selection.ranges.some((r) => !canEditInline(state, r))) return false
    const add = WRAPS[kind][0]
    const tr = state.changeByRange((range) => {
      for (const d of WRAPS[kind]) {
        if (!wrappedWith(state, range, d)) continue
        const inner = state.sliceDoc(range.from, range.to)
        // Inside the selection, or hugging it from outside: both spellings
        // of "already wrapped" have to unwrap, or the button would add a
        // second pair to text that already reads as bold.
        if (inner.startsWith(d) && inner.endsWith(d) && inner.length >= 2 * d.length) {
          return {
            changes: {
              from: range.from,
              to: range.to,
              insert: inner.slice(d.length, inner.length - d.length),
            },
            range: EditorSelection.range(range.from, range.to - 2 * d.length),
          }
        }
        return {
          changes: [
            { from: range.from - d.length, to: range.from },
            { from: range.to, to: range.to + d.length },
          ],
          range: EditorSelection.range(range.from - d.length, range.to - d.length),
        }
      }
      const inner = state.sliceDoc(range.from, range.to)
      return {
        changes: { from: range.from, to: range.to, insert: add + inner + add },
        // An empty selection leaves the caret BETWEEN the delimiters, ready
        // to type; a real one stays around the text it wrapped.
        range: range.empty
          ? EditorSelection.cursor(range.from + add.length)
          : EditorSelection.range(range.from + add.length, range.to + add.length),
      }
    })
    view.dispatch(tr, { scrollIntoView: true, userEvent: 'input.format' })
    view.focus()
    return true
  }
}

// --- line prefixes ----------------------------------------------------------

const HEADING_RE = /^(#{1,6})[ \t]+/
const BULLET_RE = /^([ \t]*)([-*+])[ \t]+(?!\[[ xX]\][ \t])/
const TASK_RE = /^([ \t]*)([-*+])[ \t]+\[[ xX]\][ \t]+/
const ORDERED_RE = /^([ \t]*)(\d+)([.)])[ \t]+/
const QUOTE_RE = /^[ \t]*>[ \t]?/
// Any list marker, so switching kind replaces rather than stacks.
const ANY_LIST_RE = /^([ \t]*)(?:[-*+][ \t]+(?:\[[ xX]\][ \t]+)?|\d+[.)][ \t]+)/

function selectedLines(state: EditorState): { from: number; to: number; text: string }[] {
  const seen = new Set<number>()
  const out: { from: number; to: number; text: string }[] = []
  for (const r of state.selection.ranges) {
    const first = state.doc.lineAt(r.from).number
    const last = state.doc.lineAt(r.to).number
    for (let n = first; n <= last; n += 1) {
      if (seen.has(n)) continue
      seen.add(n)
      const line = state.doc.line(n)
      out.push({ from: line.from, to: line.to, text: line.text })
    }
  }
  return out.sort((a, b) => a.from - b.from)
}

function applyLines(
  view: EditorView,
  map: (text: string, index: number) => string | null,
): boolean {
  const lines = selectedLines(view.state)
  const changes: ChangeSpec[] = []
  lines.forEach((line, i) => {
    const next = map(line.text, i)
    if (next === null || next === line.text) return
    changes.push({ from: line.from, to: line.to, insert: next })
  })
  if (!changes.length) return false
  view.dispatch({ changes, scrollIntoView: true, userEvent: 'input.format' })
  view.focus()
  return true
}

export function toggleHeading(level: 1 | 2 | 3) {
  return (view: EditorView): boolean => {
    const want = '#'.repeat(level) + ' '
    const lines = selectedLines(view.state)
    const allAt = lines.every((l) => l.text.startsWith(want))
    return applyLines(view, (text) => {
      const bare = text.replace(HEADING_RE, '')
      // Toggling the level a line already has removes it; any other level
      // REPLACES, so a heading never grows a second `#` run.
      return allAt ? bare : want + bare
    })
  }
}

function toggleListPrefix(
  detect: RegExp,
  prefixFor: (index: number, indent: string) => string,
) {
  return (view: EditorView): boolean => {
    const lines = selectedLines(view.state)
    const nonEmpty = lines.filter((l) => l.text.trim() !== '')
    const all = nonEmpty.length > 0 && nonEmpty.every((l) => detect.test(l.text))
    return applyLines(view, (text, i) => {
      if (text.trim() === '') return null
      if (all) return text.replace(detect, '$1')
      const indent = /^[ \t]*/.exec(text)?.[0] ?? ''
      return prefixFor(i, indent) + text.replace(ANY_LIST_RE, '$1').replace(/^[ \t]*/, '')
    })
  }
}

export const toggleBulletList = toggleListPrefix(BULLET_RE, (_i, indent) => `${indent}- `)
export const toggleTaskList = toggleListPrefix(TASK_RE, (_i, indent) => `${indent}- [ ] `)
export const toggleOrderedList = toggleListPrefix(
  ORDERED_RE,
  // Renumbered from 1 over the selection: a list whose numbers read 1, 2, 3
  // is what the author meant, whatever the source said before.
  (i, indent) => `${indent}${i + 1}. `,
)

export function toggleQuote(view: EditorView): boolean {
  const lines = selectedLines(view.state)
  const nonEmpty = lines.filter((l) => l.text.trim() !== '')
  const all = nonEmpty.length > 0 && nonEmpty.every((l) => QUOTE_RE.test(l.text))
  return applyLines(view, (text) => {
    if (text.trim() === '' && !all) return null
    return all ? text.replace(QUOTE_RE, '') : '> ' + text
  })
}

// --- block inserts ----------------------------------------------------------

/** Text that makes `insert` a block of its own wherever the caret is. */
function asOwnBlock(state: EditorState, at: number, insert: string): ChangeSpec {
  const line = state.doc.lineAt(at)
  const before = line.text.slice(0, at - line.from).trim() === '' ? '' : '\n\n'
  const after = line.text.slice(at - line.from).trim() === '' ? '' : '\n\n'
  return { from: at, insert: before + insert + after }
}

export function insertHorizontalRule(view: EditorView): boolean {
  const at = view.state.selection.main.head
  view.dispatch({
    changes: asOwnBlock(view.state, at, '---\n'),
    userEvent: 'input.format',
    scrollIntoView: true,
  })
  view.focus()
  return true
}

export function toggleCodeBlock(view: EditorView): boolean {
  const { state } = view
  const range = state.selection.main
  const body = state.sliceDoc(range.from, range.to)
  view.dispatch({
    changes: { from: range.from, to: range.to, insert: '```\n' + body + (body.endsWith('\n') ? '' : '\n') + '```\n' },
    userEvent: 'input.format',
    scrollIntoView: true,
  })
  view.focus()
  return true
}

export function insertTable(view: EditorView): boolean {
  const at = view.state.selection.main.head
  const table = ['| a | b | c |', '| --- | --- | --- |', '|  |  |  |', '|  |  |  |', ''].join('\n')
  view.dispatch({
    changes: asOwnBlock(view.state, at, table),
    userEvent: 'input.format',
    scrollIntoView: true,
  })
  view.focus()
  return true
}

/**
 * Wrap the selection as a link, or replace the destination of the one it is
 * already inside. The label goes through the shared escaper, so a title
 * containing `]` cannot emit something that is not a link.
 */
export function setLink(view: EditorView, promptFor: (current: string) => string | null): boolean {
  const { state } = view
  const range = state.selection.main
  const existing = enclosingLink(state, range.head)
  const current = existing ? state.sliceDoc(existing.destFrom, existing.destTo) : ''
  const url = promptFor(current)
  if (url === null) return false
  if (existing) {
    if (url === '') {
      // Clearing unwraps to the bare label.
      const label = state.sliceDoc(existing.labelFrom, existing.labelTo)
      view.dispatch({
        changes: { from: existing.from, to: existing.to, insert: label },
        userEvent: 'input.format',
      })
      view.focus()
      return true
    }
    view.dispatch({
      changes: { from: existing.destFrom, to: existing.destTo, insert: url },
      userEvent: 'input.format',
    })
    view.focus()
    return true
  }
  if (url === '') return false
  const label = state.sliceDoc(range.from, range.to)
  view.dispatch({
    changes: { from: range.from, to: range.to, insert: mdLink(label || url, url) },
    userEvent: 'input.format',
    scrollIntoView: true,
  })
  view.focus()
  return true
}

type LinkParts = {
  from: number
  to: number
  labelFrom: number
  labelTo: number
  destFrom: number
  destTo: number
}

/** The inline link the position sits inside, decomposed. */
export function enclosingLink(state: EditorState, pos: number): LinkParts | null {
  for (let n = syntaxTree(state).resolveInner(pos, -1); n; n = n.parent as never) {
    if (n.name === 'Link') {
      const marks: { from: number; to: number }[] = []
      let url: { from: number; to: number } | null = null
      for (let c = n.firstChild; c; c = c.nextSibling) {
        if (c.name === 'LinkMark') marks.push({ from: c.from, to: c.to })
        if (c.name === 'URL') url = { from: c.from, to: c.to }
      }
      if (marks.length < 2) return null
      return {
        from: n.from,
        to: n.to,
        labelFrom: marks[0].to,
        labelTo: marks[1].from,
        // An empty destination `[a]()` has no URL node; point at the gap
        // between the last two marks so a first destination can be written.
        destFrom: url ? url.from : n.to - 1,
        destTo: url ? url.to : n.to - 1,
      }
    }
    if (!n.parent) return null
  }
  return null
}

// --- what is active ---------------------------------------------------------

/** The constructs the caret is inside, for the toolbar's pressed state. */
export function activeMarks(state: EditorState): Set<ActiveMark> {
  const out = new Set<ActiveMark>()
  const head = state.selection.main.head
  const line = state.doc.lineAt(head)

  const heading = HEADING_RE.exec(line.text)
  if (heading) {
    const level = heading[1].length
    if (level <= 3) out.add(`heading${level}` as ActiveMark)
  }
  if (TASK_RE.test(line.text)) out.add('taskList')
  else if (BULLET_RE.test(line.text)) out.add('bulletList')
  if (ORDERED_RE.test(line.text)) out.add('orderedList')
  if (QUOTE_RE.test(line.text)) out.add('blockquote')

  for (let n = syntaxTree(state).resolveInner(head, -1); n; n = n.parent as never) {
    switch (n.name) {
      case 'StrongEmphasis':
        out.add('bold')
        break
      case 'Emphasis':
        out.add('italic')
        break
      case 'Strikethrough':
        out.add('strike')
        break
      case 'InlineCode':
        out.add('code')
        break
      case 'FencedCode':
      case 'CodeBlock':
        out.add('codeBlock')
        break
      case 'Link':
        out.add('link')
        break
      case 'Table':
        out.add('table')
        break
    }
    if (!n.parent) break
  }
  return out
}

export const undoCommand = undo
export const redoCommand = redo
