import { syntaxTree } from '@codemirror/language'
import type { ChangeSpec, EditorState, SelectionRange } from '@codemirror/state'
import { EditorSelection } from '@codemirror/state'
import type { EditorView } from '@codemirror/view'
import { redo, undo } from '@codemirror/commands'
import type { SyntaxNode } from '@lezer/common'
import { scanBlocks } from './blockScan'
import { mdLinkDestination } from '../markdownInline'

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
  // A link's destination and title. Wrapping inside one does not emphasise
  // anything, it rewrites the URL: with no selection the caret expands to the
  // word under it, and `![alt](/attachments/abc123/download)` became
  // `![alt](/attachments/**abc123**/download)`, which resolves to nothing.
  'URL',
  'LinkTitle',
])

function inOpaque(state: EditorState, pos: number, allow?: ReadonlySet<string>): boolean {
  for (let n = syntaxTree(state).resolveInner(pos, -1); n; n = n.parent as never) {
    if (OPAQUE.has(n.name) && !allow?.has(n.name)) return true
    if (!n.parent) return false
  }
  return false
}

// What the `code` command alone may touch. An inline code span is opaque to
// every OTHER inline command -- a `**` typed between backticks is two
// asterisks, not bold -- but it is the exact thing this one has to be able to
// remove. Without this the button lights up inside `codice` (activeMarks reads
// the same node) and then refuses, which reads as a broken button, and in the
// rendered view there are no visible backticks left to delete by hand.
// A fenced block's own ``` is also a CodeMark, and stays refused: its parent
// FencedCode is not on this list.
const CODE_TRANSPARENT: ReadonlySet<string> = new Set(['InlineCode', 'CodeMark'])

/**
 * May an inline command touch this range?
 *
 * Two refusals. A range crossing a BLANK LINE spans two blocks, and one pair
 * of delimiters cannot wrap two paragraphs -- the opener would close against
 * text in the next one. And a range whose ends sit inside code or a table's
 * delimiter row would gain characters that are not markup there.
 */
export function canEditInline(
  state: EditorState,
  range: SelectionRange,
  kind?: 'bold' | 'italic' | 'strike' | 'code',
): boolean {
  const allow = kind === 'code' ? CODE_TRANSPARENT : undefined
  if (inOpaque(state, range.from, allow) || inOpaque(state, range.to, allow)) return false
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

const NODE_FOR: Record<'bold' | 'italic' | 'strike' | 'code', string> = {
  bold: 'StrongEmphasis',
  italic: 'Emphasis',
  strike: 'Strikethrough',
  code: 'InlineCode',
}

/**
 * The two delimiter runs of the construct of this kind the range sits inside,
 * or null.
 *
 * Read from the syntax tree, not by looking for the delimiter either side of
 * the range. `**grassetto**` is a RUN of two asterisks, so asking "is there a
 * `*` before and a `*` after" answers yes, and the ITALIC button would take
 * one from each end and silently turn bold into italic. The parser already
 * knows which run belongs to which construct; this asks it.
 *
 * The range has to sit inside the CONTENT. One that straddles a delimiter is
 * left to the caller's string branch, which is what handles a selection that
 * INCLUDES the delimiters.
 */
function enclosingWrap(
  state: EditorState,
  range: SelectionRange,
  kind: keyof typeof WRAPS,
): { open: { from: number; to: number }; close: { from: number; to: number } } | null {
  const want = NODE_FOR[kind]
  for (let n = syntaxTree(state).resolveInner(range.from, 1); n; n = n.parent as never) {
    if (n.name === want && n.from <= range.from && n.to >= range.to) {
      const marks: { from: number; to: number }[] = []
      for (let c = n.firstChild; c; c = c.nextSibling) {
        if (c.name.endsWith('Mark')) marks.push({ from: c.from, to: c.to })
      }
      if (marks.length < 2) return null
      const open = marks[0]
      const close = marks[marks.length - 1]
      if (range.from < open.to || range.to > close.from) return null
      return { open, close }
    }
    if (!n.parent) return null
  }
  return null
}

/**
 * The range a wrap command actually operates on.
 *
 * An empty selection expands to the WORD under the caret. What it used to do
 * -- write the two delimiters and park the caret between them -- puts a pair
 * with nothing between it into the document, and an empty pair is not
 * emphasis in CommonMark: `****` mid-line is four literal asterisks, and on a
 * line of its own it is a HORIZONTAL RULE. Pressing B is not a request for
 * either, and once the markup is being drawn rather than shown, neither is
 * explicable. Null means there is no word to wrap, and the command refuses
 * rather than writing markup that parses as something else.
 */
function wrapTarget(state: EditorState, range: SelectionRange): SelectionRange | null {
  const inner = range.empty ? state.wordAt(range.head) : range
  if (!inner) return null
  // Trimmed to the text. `** parola **` is not emphasis -- a `**` followed by
  // a space is not left-flanking -- so wrapping a selection that includes its
  // own padding writes four LITERAL asterisks. Worse, there is then no node
  // for the un-toggle to find, so pressing the button again adds another
  // pair, and again, without bound. Selecting a word plus its trailing space
  // is an ordinary drag.
  const text = state.sliceDoc(inner.from, inner.to)
  const from = inner.from + (text.length - text.trimStart().length)
  const to = inner.to - (text.length - text.trimEnd().length)
  if (to <= from) return null
  return EditorSelection.range(from, to)
}

/**
 * Would adding `delim` here run into an existing delimiter of the same kind?
 *
 * `**Nota**seguito`, bolding `seguito`, gives `**Nota****seguito**`, which
 * parses as ONE StrongEmphasis whose content is the literal `Nota****seguito`:
 * the four asterisks in the middle are text, and the first word has quietly
 * stopped being bold. There is no spelling of the request that works, so the
 * command refuses and says so.
 */
function touchesSameDelimiter(
  state: EditorState,
  range: SelectionRange,
  delim: string,
): boolean {
  const ch = delim[0]
  return (
    state.sliceDoc(Math.max(0, range.from - 1), range.from) === ch ||
    state.sliceDoc(range.to, Math.min(state.doc.length, range.to + 1)) === ch
  )
}

/** Toggle an inline wrap over every selection range. */
export function toggleWrap(kind: keyof typeof WRAPS) {
  return (view: EditorView): boolean => {
    const { state } = view
    const targets = state.selection.ranges.map((r) => wrapTarget(state, r))
    if (targets.some((r) => r === null)) return false
    if (targets.some((r) => !canEditInline(state, r as SelectionRange, kind))) return false
    const add = WRAPS[kind][0]
    if (
      targets.some(
        (r) =>
          !enclosingWrap(state, r as SelectionRange, kind) &&
          touchesSameDelimiter(state, r as SelectionRange, add),
      )
    ) {
      return false
    }
    const tr = state.changeByRange((sel) => {
      const range = wrapTarget(state, sel) as SelectionRange
      const found = enclosingWrap(state, range, kind)
      if (found) {
        // Remove the construct's own delimiters, whatever they are spelled
        // as: `__bold__` and `**bold**` are one node each, and the node knows
        // how long its marks are.
        const shift = found.open.to - found.open.from
        return {
          changes: [
            { from: found.open.from, to: found.open.to },
            { from: found.close.from, to: found.close.to },
          ],
          range: sel.empty
            ? EditorSelection.cursor(sel.head - shift)
            : EditorSelection.range(range.from - shift, range.to - shift),
        }
      }
      const inner = state.sliceDoc(range.from, range.to)
      for (const d of WRAPS[kind]) {
        // The selection INCLUDES the delimiters: strip them from the text
        // rather than from around it. Both spellings are recognised, because
        // content written by the previous editor used `*` for italic.
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
      }
      return {
        changes: { from: range.from, to: range.to, insert: add + inner + add },
        // An expanded caret stays a caret, where it was, now inside the
        // wrap; a real selection stays around the text it wrapped.
        range: sel.empty
          ? EditorSelection.cursor(sel.head + add.length)
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
    // Numbered from the lines actually PREFIXED, not from the index into the
    // selection. A blank line is skipped (the `null` above) but still occupied
    // an index, so a selection starting on one used to begin at `2.`.
    let n = 0
    return applyLines(view, (text) => {
      if (text.trim() === '') return null
      if (all) return text.replace(detect, '$1')
      const indent = /^[ \t]*/.exec(text)?.[0] ?? ''
      const prefix = prefixFor(n, indent)
      n += 1
      return prefix + text.replace(ANY_LIST_RE, '$1').replace(/^[ \t]*/, '')
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

/** Text that makes `insert` a block of its own wherever the caret is.
 *
 *  ``state.lineBreak``, never a bare ``\n``: CodeMirror splits an inserted
 *  string on the document's OWN separator, so a `\n` written into a CRLF body
 *  survives as a literal character inside a line. The document then MIXES its
 *  line endings, which is the one shape this editor cannot keep exact. */
function asOwnBlock(state: EditorState, at: number, insert: string): ChangeSpec {
  const gap = state.lineBreak + state.lineBreak
  const line = state.doc.lineAt(at)
  const before = line.text.slice(0, at - line.from).trim() === '' ? '' : gap
  const after = line.text.slice(at - line.from).trim() === '' ? '' : gap
  return { from: at, insert: before + insert + after }
}

export function insertHorizontalRule(view: EditorView): boolean {
  const at = view.state.selection.main.head
  view.dispatch({
    changes: asOwnBlock(view.state, at, '---' + view.state.lineBreak),
    userEvent: 'input.format',
    scrollIntoView: true,
  })
  view.focus()
  return true
}

/**
 * The fenced block the position sits in, from the LINE SCAN rather than the
 * syntax tree.
 *
 * `resolveInner(pos, -1)` looks to the LEFT of the position, so at the first
 * column of the opening fence line it resolves into whatever block precedes
 * the fence and answers null -- and the command then nested a second fence
 * inside the first. The scan is also the only one of the two that says where
 * the content ends, which is what tells a closed fence from an unclosed one.
 */
function enclosingFence(state: EditorState, pos: number) {
  for (const b of scanBlocks(state.doc)) {
    if (b.kind !== 'fence') continue
    if (pos < b.from || pos > b.to) continue
    return b
  }
  return null
}

export function toggleCodeBlock(view: EditorView): boolean {
  const { state } = view
  const range = state.selection.main
  // Already inside a fence: take it off. Without this the button could only
  // ever go one way -- it reported itself pressed (activeMarks reads the same
  // node) and then nested a second pair inside the first. An INDENTED code
  // block is deliberately not handled here: removing it is a dedent, which is
  // a different operation from deleting two fence lines.
  const fence = enclosingFence(state, range.head)
  if (fence) {
    const first = state.doc.lineAt(fence.from)
    const last = state.doc.lineAt(fence.to)
    const changes: ChangeSpec[] = [
      { from: first.from, to: Math.min(first.to + 1, state.doc.length) },
    ]
    // A CLOSING line, not merely the last one. An unterminated fence ends at
    // its last line of CONTENT, which must not be deleted; and a closer is
    // spelled with the same character the opener used, so a `~~~` line inside
    // a ``` fence is content too.
    const opener = first.text.trimStart()[0] ?? ''
    const closed =
      last.number > first.number &&
      last.from >= fence.contentTo &&
      last.text.trimStart().startsWith(opener.repeat(3))
    if (closed) {
      // The closing line and the line break before it.
      changes.push({ from: Math.max(last.from - 1, first.to + 1), to: last.to })
    }
    view.dispatch({ changes, userEvent: 'input.format' })
    view.focus()
    return true
  }
  const nl = state.lineBreak
  const body = state.sliceDoc(range.from, range.to)
  view.dispatch({
    changes: {
      from: range.from,
      to: range.to,
      insert: '```' + nl + body + (body.endsWith(nl) ? '' : nl) + '```' + nl,
    },
    userEvent: 'input.format',
    scrollIntoView: true,
  })
  view.focus()
  return true
}

export function insertTable(view: EditorView): boolean {
  const at = view.state.selection.main.head
  const table = ['| a | b | c |', '| --- | --- | --- |', '|  |  |  |', '|  |  |  |', ''].join(
    view.state.lineBreak,
  )
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
  // A link label may legally wrap in CommonMark, but the only way to emit one
  // that does is to keep the newline, and the emitter collapses it to a
  // space -- which changes bytes outside the construct the author selected.
  // Refuse, and say so.
  if (/\r?\n/.test(label)) return false
  view.dispatch({
    changes: {
      from: range.from,
      to: range.to,
      insert: `[${escapeLabelFromSource(label || url)}](${mdLinkDestination(url)})`,
    },
    userEvent: 'input.format',
    scrollIntoView: true,
  })
  view.focus()
  return true
}

/**
 * Escape brackets in text taken OUT of the document.
 *
 * `mdLinkLabel` is the escaper for RAW text -- an uploaded filename, a task
 * title -- and it doubles every backslash on the way in. Run over markdown
 * SOURCE, which is already escaped, it escalates: `a \] b` comes back as
 * `a \\\] b`, and the next press doubles it again. Backslashes multiplying
 * through a body is the exact failure the previous editor was retired for, so
 * this one copies an existing escape verbatim and only escapes a BARE
 * bracket. `mdLinkLabel` is left alone for its raw-text callers, and for the
 * two mirrored copies in the Python core and the CLI.
 */
function escapeLabelFromSource(text: string): string {
  let out = ''
  for (let i = 0; i < text.length; i += 1) {
    const c = text[i]
    if (c === '\\') {
      // A DANGLING backslash has to be escaped, not copied: it would escape
      // the `]` the caller appends and the result would parse as text, with
      // no Link node -- and no way to repair it with the same button, since
      // `enclosingLink` would never find one.
      out += i + 1 < text.length ? c + text[i + 1] : '\\\\'
      i += 1
      continue
    }
    out += c === '[' || c === ']' ? '\\' + c : c
  }
  return out
}

/**
 * Is this `Link` node an INLINE link, `[label](destination)`?
 *
 * lezer emits a `Link` for any bracket run, whether or not anything defines
 * it: `see [proven] status`, `array[0]`, `[a][ref]`, `[b][]` and a footnote
 * reference `[^1]` are all `Link` nodes with two `LinkMark` children and no
 * destination. None of them is a link here. Their meaning, where they have
 * one, lives in a definition this editor does not resolve -- and neither does
 * the reader, which prints them literally.
 *
 * Taking them for links had two visible consequences: the link button spliced
 * a URL in before the `]` (`see [proven] status` became
 * `see [provenhttps://x] status`, no link produced), and the preview layer
 * hid brackets the reader shows, drawing `array[0]` as `array0`.
 *
 * The test is the character after the closing bracket, which is what
 * distinguishes an inline link from every other bracket construct. `[a]()`
 * counts: it is a link with nowhere to go, and giving it a destination is
 * exactly what the button is for.
 */
export function isInlineLink(state: EditorState, link: SyntaxNode): boolean {
  const marks: SyntaxNode[] = []
  for (let c = link.firstChild; c; c = c.nextSibling) {
    if (c.name === 'LinkMark') marks.push(c)
  }
  if (marks.length < 2) return false
  return state.sliceDoc(marks[1].to, marks[1].to + 1) === '('
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
      if (!isInlineLink(state, n)) return null
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
