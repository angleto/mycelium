import type { EditorState, Extension } from '@codemirror/state'
import { EditorSelection } from '@codemirror/state'
import { EditorView, keymap } from '@codemirror/view'
import { Prec } from '@codemirror/state'
import { scanBlocks } from './blockScan'
import { escapeCell, rowAlignments, splitRow } from './widgets'

// Editing a GFM table as source.
//
// The tiptap surface gave a grid: Tab between cells, buttons for rows and
// columns. It also, measurably, destroyed tables -- editing one cell of an
// aligned table dropped the alignment row, a cell holding only an image was
// deleted, an escaped `\|` truncated the row, and pressing Enter in a cell
// replaced the whole table with the literal string `[table]`. So the trade
// here is a real one in both directions, and it is worth stating plainly:
// grid navigation for correctness.
//
// What is kept is Tab/Shift-Tab across cells, add row, add column, delete
// table, and an explicit re-align. What is deliberately NOT kept is
// automatic reformatting: `formatTable` is a button the author presses, never
// something that fires on Tab. Rewriting bytes nobody typed is the category
// of behaviour this whole substrate exists to remove, and a table is not an
// exception to it.

type TableAt = { from: number; to: number; lines: string[] }

/** The table the position sits in, if any. */
export function tableAt(state: EditorState, pos: number): TableAt | null {
  for (const b of scanBlocks(state.doc)) {
    if (b.kind !== 'table') continue
    if (pos < b.from || pos > b.to) continue
    // Split on the DOCUMENT's separator: `sliceDoc` hands back CRLF for a
    // CRLF body, and splitting that on `\n` leaves a `\r` on the end of every
    // line -- which then travels into the cell text, into the measured widths
    // and into everything rewritten from them.
    return {
      from: b.from,
      to: b.to,
      lines: state.sliceDoc(b.from, b.to).split(state.lineBreak),
    }
  }
  return null
}

/** Cell boundaries of one row line, in document positions. Only the cells:
 *  the pipes themselves are not part of any of them. */
function cellRanges(lineText: string, lineFrom: number): { from: number; to: number }[] {
  const out: { from: number; to: number }[] = []
  let escaped = false
  // Skip a leading pipe, which delimits rather than opening a cell.
  const lead = /^[ \t]*\|/.exec(lineText)
  let i = lead ? lead[0].length : 0
  let cellStart = i
  for (; i < lineText.length; i += 1) {
    const c = lineText[i]
    if (escaped) {
      escaped = false
      continue
    }
    if (c === '\\') {
      escaped = true
      continue
    }
    if (c === '|') {
      out.push({ from: lineFrom + cellStart, to: lineFrom + i })
      cellStart = i + 1
    }
  }
  // Trailing pipe already closed the last cell; otherwise the rest is one.
  if (cellStart < lineText.length) {
    out.push({ from: lineFrom + cellStart, to: lineFrom + lineText.length })
  }
  return out
}

/** Every cell of the table, in reading order, skipping the delimiter row. */
function cellsOf(t: TableAt): { from: number; to: number }[] {
  const out: { from: number; to: number }[] = []
  let at = t.from
  t.lines.forEach((text, i) => {
    if (i !== 1) out.push(...cellRanges(text, at))
    at += text.length + 1
  })
  return out
}

function moveCell(dir: 1 | -1) {
  return (view: EditorView): boolean => {
    const { state } = view
    const pos = state.selection.main.head
    const t = tableAt(state, pos)
    if (!t) return false
    const cells = cellsOf(t)
    if (!cells.length) return false
    let idx = cells.findIndex((c) => pos >= c.from && pos <= c.to)
    if (idx < 0) idx = dir > 0 ? -1 : cells.length
    const next = idx + dir
    if (next < 0 || next >= cells.length) return false
    const cell = cells[next]
    // Select the cell's content (trimmed), so typing replaces it -- the one
    // grid affordance worth keeping, and it needs no reformatting to work.
    const text = state.sliceDoc(cell.from, cell.to)
    const lead = /^[ \t]*/.exec(text)?.[0].length ?? 0
    const trail = /[ \t]*$/.exec(text)?.[0].length ?? 0
    view.dispatch({
      selection: EditorSelection.range(cell.from + lead, cell.to - trail),
      scrollIntoView: true,
    })
    return true
  }
}

export const nextCell = moveCell(1)
export const prevCell = moveCell(-1)

function columnCount(delimiterLine: string): number {
  return splitRow(delimiterLine).length
}

export function addRowAfter(view: EditorView): boolean {
  const { state } = view
  const pos = state.selection.main.head
  const t = tableAt(state, pos)
  if (!t) return false
  const cols = columnCount(t.lines[1] ?? '')
  const line = state.doc.lineAt(pos)
  // Never above the delimiter row: a row inserted there would become the
  // header separator and the table would lose its shape.
  const headerEnd = state.doc.lineAt(t.from).number + 1
  const at = Math.max(line.number, headerEnd)
  const insertAfter = state.doc.line(at).to
  view.dispatch({
    changes: { from: insertAfter, insert: state.lineBreak + '|' + '  |'.repeat(cols) },
    userEvent: 'input.format',
    scrollIntoView: true,
  })
  view.focus()
  return true
}

export function addColumnAfter(view: EditorView): boolean {
  const { state } = view
  const t = tableAt(state, state.selection.main.head)
  if (!t) return false
  const changes: { from: number; to: number; insert: string }[] = []
  let at = t.from
  t.lines.forEach((text, i) => {
    const trimmed = text.replace(/[ \t]+$/, '')
    // A row that does not already end with a pipe gets one, so appending a
    // cell cannot merge into the last existing one.
    const base = trimmed.endsWith('|') ? trimmed : trimmed + ' |'
    changes.push({
      from: at,
      to: at + text.length,
      insert: base + (i === 1 ? ' --- |' : '   |'),
    })
    at += text.length + 1
  })
  view.dispatch({ changes, userEvent: 'input.format', scrollIntoView: true })
  view.focus()
  return true
}

export function deleteTable(view: EditorView): boolean {
  const { state } = view
  const t = tableAt(state, state.selection.main.head)
  if (!t) return false
  const firstLine = state.doc.lineAt(t.from).number
  const lastLine = state.doc.lineAt(t.to).number
  let to = Math.min(state.doc.length, state.doc.line(lastLine).to + 1)
  // A table sitting between two blank lines leaves one blank line behind if
  // only its own lines go: the separator above and the separator below would
  // become a double gap. Take the one below in that case, so the blocks that
  // surrounded it end up separated exactly as they were.
  const blankAbove = firstLine > 1 && state.doc.line(firstLine - 1).text.trim() === ''
  const blankBelow =
    lastLine + 1 <= state.doc.lines && state.doc.line(lastLine + 1).text.trim() === ''
  if (blankAbove && blankBelow) {
    to = Math.min(state.doc.length, state.doc.line(lastLine + 1).to + 1)
  }
  view.dispatch({ changes: { from: t.from, to }, userEvent: 'delete' })
  view.focus()
  return true
}

/**
 * Re-pad the pipes so the columns line up, PRESERVING the alignment markers
 * (`:--`, `--:`, `:-:`) that the previous editor silently dropped.
 *
 * Explicitly a command, never automatic. It rewrites bytes the author did
 * not type, which is only acceptable when the author asked for it.
 */
export function formatTable(view: EditorView): boolean {
  const { state } = view
  const t = tableAt(state, state.selection.main.head)
  if (!t) return false
  // ESCAPED cells throughout: `splitRow` unescapes `\|` so a cell reads as
  // its text, and joining that back with ` | ` would separate on a pipe the
  // author had escaped -- the row gains a cell, the last one is destroyed,
  // and the widths would have been measured on the wrong strings too.
  const rows = t.lines.map((l, i) => (i === 1 ? null : splitRow(l).map(escapeCell)))
  const align = rowAlignments(t.lines[1] ?? '')
  const cols = Math.max(...rows.map((r) => r?.length ?? 0), align.length)
  // A column is as wide as its widest cell OR its delimiter, whichever is
  // larger: `:---` is four characters, so a column of one-character cells
  // whose width was capped at three would leave the delimiter row sticking
  // out and the whole point of re-padding lost.
  const delimWidth = (a: (typeof align)[number]) => 3 + (a === 'center' ? 2 : a ? 1 : 0)
  const width: number[] = []
  for (let c = 0; c < cols; c += 1) {
    let w = delimWidth(align[c] ?? null)
    for (const r of rows) w = Math.max(w, r?.[c]?.length ?? 0)
    width.push(w)
  }
  const pad = (s: string, c: number) => s + ' '.repeat(Math.max(0, width[c] - s.length))
  const out = t.lines.map((_l, i) => {
    if (i === 1) {
      const cells = align.map((a, c) => {
        const dashes = '-'.repeat(Math.max(3, width[c] - (a === 'center' ? 2 : a ? 1 : 0)))
        if (a === 'center') return `:${dashes}:`
        if (a === 'right') return `${dashes}:`
        if (a === 'left') return `:${dashes}`
        return dashes
      })
      return '| ' + cells.join(' | ') + ' |'
    }
    const cells = rows[i] ?? []
    return (
      '| ' +
      Array.from({ length: cols }, (_v, c) => pad(cells[c] ?? '', c)).join(' | ') +
      ' |'
    )
  })
  view.dispatch({
    changes: { from: t.from, to: t.to, insert: out.join(state.lineBreak) },
    userEvent: 'input.format',
  })
  view.focus()
  return true
}

/**
 * Tab / Shift-Tab move between cells INSIDE a table and do nothing anywhere
 * else, so the key keeps its accessibility meaning (move focus out of the
 * editor) everywhere it is not obviously a table navigation.
 */
export function tableKeymap(): Extension {
  return Prec.high(
    keymap.of([
      { key: 'Tab', run: nextCell },
      { key: 'Shift-Tab', run: prevCell },
    ]),
  )
}
