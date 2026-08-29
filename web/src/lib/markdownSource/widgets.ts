import { WidgetType, type EditorView } from '@codemirror/view'
import { createRoot, type Root } from 'react-dom/client'
import { createElement, type ReactNode } from 'react'
import katex from 'katex'
import 'katex/dist/katex.min.css'
import i18n from '../../i18n'
import { Mermaid } from '../../components/Mermaid'
import { AttachmentImage } from './AttachmentImage'
import type { ImageUploadParent } from '../imageUpload'

// The block widgets the live-preview layer puts in place of a construct's
// source: a rendered diagram, a typeset formula, a real table.
//
// Every one of them obeys the same three rules.
//
// `eq()` keys on the SOURCE SLICE the widget was built from. CodeMirror
// reuses a widget whose `eq` says it is unchanged, so without this a
// keystroke anywhere in the document would tear down and rebuild every
// diagram on screen -- mermaid would re-parse and re-render each one, and
// the page would visibly flash.
//
// `ignoreEvent()` returns true, so clicking inside a widget does not move
// the editor selection into a position that does not exist in the document.
//
// `destroy()` releases whatever the widget holds. For the React-backed ones
// that means unmounting the root, and it has to happen in a microtask:
// React refuses a synchronous unmount from inside a commit, which is exactly
// where CodeMirror calls destroy from.

const roots = new WeakMap<HTMLElement, Root>()

/**
 * Click a widget to get its source back.
 *
 * A widget stands where several lines of markdown would be, and it is
 * `contentEditable=false`, so without this there is no way to put the caret
 * inside a rendered table, diagram or formula -- and with no caret there,
 * `tableAt` never matches and Tab, +row, +col, delete-table and re-align all
 * stop existing.
 *
 * It dispatches a SELECTION and nothing else. The shared reveal
 * (reveal.ts) then shows that block's source, so there is exactly one reveal
 * rule and no second one for the two layers to disagree about. Never a
 * document change: what comes back is the bytes that were always there.
 *
 * `ignoreEvent()` staying true is not in tension with this. It tells
 * CodeMirror not to interpret the event as its own; this listener is ours and
 * still runs.
 */
function revealOnClick(dom: HTMLElement, view: EditorView): void {
  dom.title = i18n.t('editor.revealBlock')
  dom.addEventListener('mousedown', (event) => {
    // The PRIMARY button only. Preventing the default on a secondary click
    // suppresses the context menu, so right-clicking a diagram to copy it
    // would do nothing but move the caret.
    if (event.button !== 0) return
    event.preventDefault()
    view.dispatch({ selection: { anchor: view.posAtDOM(dom) } })
    view.focus()
  })
}

/** A widget whose content is a React element rendered into its own root. */
abstract class ReactWidget extends WidgetType {
  /** Identity for `eq`: the source this widget was built from. */
  abstract readonly key: string
  protected abstract render(): ReactNode
  protected abstract className: string
  /** Whether this widget stands IN PLACE of its source (so clicking it has to
   *  bring the source back) or merely beside it, which is what a diagram or a
   *  formula does while its fence is being edited. */
  readonly replaces: boolean

  constructor(replaces = true) {
    super()
    this.replaces = replaces
  }

  eq(other: WidgetType): boolean {
    return (
      other instanceof ReactWidget &&
      other.constructor === this.constructor &&
      other.key === this.key &&
      other.replaces === this.replaces
    )
  }

  toDOM(view: EditorView): HTMLElement {
    const dom = document.createElement('div')
    dom.className = this.className
    dom.contentEditable = 'false'
    const root = createRoot(dom)
    roots.set(dom, root)
    root.render(this.render())
    if (this.replaces) revealOnClick(dom, view)
    return dom
  }

  destroy(dom: HTMLElement): void {
    const root = roots.get(dom)
    if (!root) return
    roots.delete(dom)
    queueMicrotask(() => root.unmount())
  }

  ignoreEvent(): boolean {
    return true
  }
}

/** A ```mermaid fence, as its diagram. */
export class MermaidWidget extends ReactWidget {
  protected className = 'cm-md-widget cm-md-mermaid'
  // Explicit field rather than a parameter property: this project builds
  // with ``erasableSyntaxOnly``, which rules out the shorthand.
  readonly key: string

  constructor(key: string, replaces = true) {
    super(replaces)
    this.key = key
  }

  protected render(): ReactNode {
    // ``createElement`` rather than JSX so this module can be a plain .ts
    // file. A .tsx exporting classes and helpers instead of components trips
    // react-refresh/only-export-components, and splitting the widgets across
    // two files to satisfy a lint rule would be worse than one call.
    return createElement(Mermaid, { code: this.key })
  }
}

/** A `$$ … $$` block, typeset. No React: KaTeX renders straight into a
 *  node, so a root would be pure overhead. */
export class MathWidget extends WidgetType {
  readonly tex: string
  readonly replaces: boolean

  constructor(tex: string, replaces = true) {
    super()
    this.tex = tex
    this.replaces = replaces
  }

  eq(other: WidgetType): boolean {
    return other instanceof MathWidget && other.tex === this.tex && other.replaces === this.replaces
  }

  toDOM(view: EditorView): HTMLElement {
    const dom = document.createElement('div')
    dom.className = 'cm-md-widget cm-md-math'
    dom.contentEditable = 'false'
    try {
      katex.render(this.tex, dom, {
        displayMode: true,
        throwOnError: false,
        output: 'htmlAndMathml',
        strict: 'ignore',
      })
    } catch {
      // A half-typed formula is the normal case while writing, not an error
      // worth a red box: fall back to the source the author is editing.
      dom.textContent = `$$${this.tex}$$`
      dom.classList.add('cm-md-math--error')
    }
    if (this.replaces) revealOnClick(dom, view)
    return dom
  }

  ignoreEvent(): boolean {
    return true
  }
}

/** Split a GFM table row into cells, honouring `\|` escapes.
 *  An unescaped pipe separates; `\|` is a literal pipe inside a cell. This
 *  is the case the tiptap serializer got wrong (it unescaped the pipe and
 *  the row gained a cell, destroying the last one). */
export function splitRow(line: string): string[] {
  const cells: string[] = []
  let cur = ''
  let i = 0
  // Leading and trailing pipes are optional delimiters, not empty cells.
  let s = line.trim()
  if (s.startsWith('|')) s = s.slice(1)
  if (s.endsWith('|') && !s.endsWith('\\|')) s = s.slice(0, -1)
  while (i < s.length) {
    const c = s[i]
    if (c === '\\' && s[i + 1] === '|') {
      cur += '|'
      i += 2
      continue
    }
    if (c === '|') {
      cells.push(cur.trim())
      cur = ''
      i += 1
      continue
    }
    cur += c
    i += 1
  }
  cells.push(cur.trim())
  return cells
}

/**
 * The inverse of `splitRow`'s unescaping.
 *
 * `splitRow` turns `\|` into a literal `|` so a cell reads as its text. Any
 * path that JOINS cells back into a row has to undo that, or the pipe
 * separates and the row silently gains a cell: `| a \| b | escaped |`
 * becomes a three-cell row against a two-column delimiter and the last cell
 * is destroyed. That is precisely the corruption the retired serializer
 * shipped (see the note at the top of tableCommands.ts).
 */
export function escapeCell(cell: string): string {
  return cell.replace(/\|/g, '\\|')
}

/** Column alignments from a GFM delimiter row. */
export function rowAlignments(delim: string): ('left' | 'center' | 'right' | null)[] {
  return splitRow(delim).map((c) => {
    const left = c.startsWith(':')
    const right = c.endsWith(':')
    if (left && right) return 'center'
    if (right) return 'right'
    if (left) return 'left'
    return null
  })
}

/** A GFM table, as a table. Built from the source directly rather than
 *  through the markdown renderer: the cells hold inline markup that this
 *  layer does not resolve, and showing it as text is honest -- clicking in
 *  gives the source anyway. */
export class TableWidget extends WidgetType {
  readonly source: string

  constructor(source: string) {
    super()
    this.source = source
  }

  eq(other: WidgetType): boolean {
    return other instanceof TableWidget && other.source === this.source
  }

  toDOM(view: EditorView): HTMLElement {
    const dom = document.createElement('div')
    dom.className = 'cm-md-widget cm-md-table'
    dom.contentEditable = 'false'
    const lines = this.source.split('\n')
    const table = document.createElement('table')
    const align = lines.length > 1 ? rowAlignments(lines[1]) : []
    const head = document.createElement('thead')
    const headRow = document.createElement('tr')
    splitRow(lines[0] ?? '').forEach((cell, i) => {
      const th = document.createElement('th')
      th.textContent = cell
      if (align[i]) th.style.textAlign = align[i] as string
      headRow.append(th)
    })
    head.append(headRow)
    table.append(head)
    const body = document.createElement('tbody')
    for (const line of lines.slice(2)) {
      const tr = document.createElement('tr')
      splitRow(line).forEach((cell, i) => {
        const td = document.createElement('td')
        td.textContent = cell
        if (align[i]) td.style.textAlign = align[i] as string
        tr.append(td)
      })
      body.append(tr)
    }
    table.append(body)
    dom.append(table)
    revealOnClick(dom, view)
    return dom
  }

  ignoreEvent(): boolean {
    return true
  }
}


/**
 * An `![alt](src "title")` embed, as the image.
 *
 * `eq()` keys on the whole source slice, so a widget survives a keystroke
 * elsewhere untouched; the refcounted blob cache then never re-fetches. What
 * it does NOT protect against is the caret moving onto the image's own line,
 * which reveals the source and destroys the widget for real -- that is what
 * `attachmentRetain` is for.
 */
export class ImageWidget extends ReactWidget {
  protected className = 'cm-md-widget-inline'
  readonly key: string
  private readonly src: string
  private readonly alt: string
  private readonly title: string | undefined
  private readonly getParent: () => ImageUploadParent | undefined

  constructor(opts: {
    source: string
    src: string
    alt: string
    title?: string
    getParent: () => ImageUploadParent | undefined
  }) {
    super()
    this.key = opts.source
    this.src = opts.src
    this.alt = opts.alt
    this.title = opts.title
    this.getParent = opts.getParent
  }

  protected render(): ReactNode {
    return createElement(AttachmentImage, {
      src: this.src,
      alt: this.alt,
      title: this.title,
      parent: this.getParent(),
    })
  }
}

// `![alt](dest "title")`, whole and on one line. Deliberately the same shape
// the rest of the app parses (attachmentRef.ts): a bracket-free-or-escaped
// label, an optional angle-bracketed destination, an optional quoted title.
const IMAGE_RE =
  /^!\[((?:\\[\s\S]|[^\\\]\n])*)\]\(\s*(<[^<>\n]*>|[^\s()]*)\s*(?:"([^"\n]*)"|'([^'\n]*)')?\s*\)$/

/** Decompose an image embed, or null when the slice is not exactly one. */
export function parseImageEmbed(
  source: string,
): { src: string; alt: string; title?: string } | null {
  const m = IMAGE_RE.exec(source)
  if (!m) return null
  const dest = m[2].startsWith('<') && m[2].endsWith('>') ? m[2].slice(1, -1) : m[2]
  return {
    src: dest.replace(/\\([\s\S])/g, '$1'),
    alt: m[1].replace(/\\([\s\S])/g, '$1'),
    title: m[3] ?? m[4],
  }
}
