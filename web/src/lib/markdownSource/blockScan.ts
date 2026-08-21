import type { Text } from '@codemirror/state'

// Where the block-level constructs are, found by scanning LINES rather than
// by walking the syntax tree.
//
// That is a deliberate choice, not a shortcut. CodeMirror time-slices
// parsing, so `syntaxTree(state)` is only guaranteed over the viewport, and a
// block widget declares `block: true`, which means it determines the height
// of the lines it replaces. Deriving block widgets from a viewport-limited
// tree makes the document's total height change as parsing catches up, and
// the scroll position jumps under the reader. Every construct here is
// line-anchored and unambiguous from the line text alone, so an O(lines)
// scan over the whole document costs less than the bug it avoids.
//
// Inline constructs still come from the tree (livePreview.ts), where the
// viewport limit is exactly right: an inline decoration that is missing
// off-screen changes nothing about layout.

export type ScannedBlock =
  | {
      kind: 'fence'
      from: number
      to: number
      /** The info string after the opening run, trimmed. `mermaid`, `js`, ''. */
      info: string
      /** The fenced content, without the fence lines. */
      contentFrom: number
      contentTo: number
    }
  | { kind: 'math'; from: number; to: number; tex: string }
  | { kind: 'table'; from: number; to: number }
  | {
      kind: 'setext'
      /** The heading text line. */
      from: number
      /** End of the underline line. */
      to: number
      level: 1 | 2
      /** Start of the underline line, i.e. what has to be folded away. */
      underlineFrom: number
    }

const FENCE_OPEN = /^ {0,3}(`{3,}|~{3,})(.*)$/
const MATH_OPEN = /^ {0,3}\$\$/
const SETEXT = /^ {0,3}(=+|-+)[ \t]*$/
// A GFM delimiter row: pipes, dashes, colons and spaces, with at least one
// dash. `| --- | :-: |`, `---|---`, `|:--|--:|`.
const TABLE_DELIM = /^ {0,3}\|?[ \t]*:?-+:?[ \t]*(\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*$/

function isBlank(text: string): boolean {
  return text.trim() === ''
}

/**
 * Every block-level construct in `doc`, in document order, non-overlapping.
 *
 * Fences win over everything: their content is literal, so a `$$` or a `|`
 * inside one is text and must not be scanned. That is the single ordering
 * rule the rest of the function depends on.
 */
export function scanBlocks(doc: Text): ScannedBlock[] {
  const out: ScannedBlock[] = []
  const total = doc.lines
  let n = 1
  while (n <= total) {
    const line = doc.line(n)
    const text = line.text

    const fence = FENCE_OPEN.exec(text)
    if (fence) {
      const marker = fence[1]
      const char = marker[0]
      const info = fence[2].trim()
      // A backtick fence's info string may not contain a backtick
      // (CommonMark), which is what keeps `` `a` `` on its own line from
      // opening one.
      if (!(char === '`' && info.includes('`'))) {
        const openTo = line.to
        let close = -1
        let m = n + 1
        for (; m <= total; m += 1) {
          const t = doc.line(m).text
          const c = new RegExp(`^ {0,3}${char === '`' ? '`' : '~'}{${marker.length},}[ \\t]*$`)
          if (c.test(t)) {
            close = m
            break
          }
        }
        // An unclosed fence runs to the end of the document, which is what
        // CommonMark says and what the reader will do with it.
        const lastLine = close > 0 ? close : total
        const contentFrom = n + 1 <= total ? doc.line(n + 1).from : openTo
        const contentTo = close > 0 ? (close - 1 >= n + 1 ? doc.line(close - 1).to : contentFrom) : doc.line(total).to
        out.push({
          kind: 'fence',
          from: line.from,
          to: doc.line(lastLine).to,
          info,
          contentFrom,
          contentTo: Math.max(contentFrom, contentTo),
        })
        n = lastLine + 1
        continue
      }
    }

    if (MATH_OPEN.test(text)) {
      const afterOpen = text.replace(/^ {0,3}\$\$/, '')
      // Single-line form: `$$ x^2 $$`.
      if (afterOpen.trimEnd().endsWith('$$') && afterOpen.trim() !== '$$'.slice(0, 0)) {
        const inner = afterOpen.trimEnd().slice(0, -2).trim()
        if (inner) {
          out.push({ kind: 'math', from: line.from, to: line.to, tex: inner })
          n += 1
          continue
        }
      }
      let close = -1
      for (let m = n + 1; m <= total; m += 1) {
        if (doc.line(m).text.trimEnd().endsWith('$$')) {
          close = m
          break
        }
      }
      if (close > 0) {
        const parts: string[] = []
        const head = afterOpen.trim()
        if (head) parts.push(head)
        for (let m = n + 1; m < close; m += 1) parts.push(doc.line(m).text)
        const tail = doc.line(close).text.trimEnd().slice(0, -2).trimEnd()
        if (tail.trim()) parts.push(tail)
        out.push({
          kind: 'math',
          from: line.from,
          to: doc.line(close).to,
          tex: parts.join('\n').trim(),
        })
        n = close + 1
        continue
      }
    }

    // A table needs its delimiter row on the NEXT line; without it a row of
    // pipes is just a paragraph.
    if (text.includes('|') && n + 1 <= total && TABLE_DELIM.test(doc.line(n + 1).text)) {
      let last = n + 1
      for (let m = n + 2; m <= total; m += 1) {
        const t = doc.line(m).text
        if (isBlank(t) || !t.includes('|')) break
        last = m
      }
      out.push({ kind: 'table', from: line.from, to: doc.line(last).to })
      n = last + 1
      continue
    }

    // A setext underline can only follow a paragraph line, so the previous
    // line has to exist, be non-blank, and not have been eaten by a block
    // above (which the `continue`s guarantee: we only reach here line by
    // line through ordinary paragraph text).
    const setext = SETEXT.exec(text)
    if (setext && n > 1) {
      const prev = doc.line(n - 1)
      const prevConsumed = out.some((b) => b.to >= prev.from)
      if (!isBlank(prev.text) && !prevConsumed && !TABLE_DELIM.test(prev.text)) {
        out.push({
          kind: 'setext',
          from: prev.from,
          to: line.to,
          level: setext[1][0] === '=' ? 1 : 2,
          underlineFrom: line.from,
        })
        n += 1
        continue
      }
    }

    n += 1
  }
  return out
}
