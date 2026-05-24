import { Node, mergeAttributes, nodeInputRule } from '@tiptap/core'
import katex from 'katex'
import 'katex/dist/katex.min.css'

// Round-trip LaTeX support inside the WYSIWYG editor. The renderer side
// (/garden's MarkdownView) already understands `$inline$` and `$$block$$`
// via remark-math + rehype-katex; this module gives the tiptap editor
// the same capability so what the author types is what /garden shows.
//
// Architecture:
//   - Two atom nodes (`inlineMath`, `blockMath`) holding the TeX source
//     in a `tex` attribute. A NodeView renders the formula via
//     ``katex.render`` so the user sees the typeset output in-editor.
//   - tiptap-markdown integration via ``addStorage().markdown``:
//       * serialize: emit ``$tex$`` / ``$$\n tex \n$$`` so the stored
//         markdown is identical to what remark-math expects.
//       * parse.setup: register markdown-it rules that turn the same
//         markdown back into ``<span data-inline-math>`` / ``<div
//         data-block-math>`` carriers picked up by ``parseHTML``.
//   - An InputRule fires when the user finishes a ``$...$`` span while
//     typing, so the formula renders without leaving the editor.

function renderTexInto(dom: HTMLElement, tex: string, displayMode: boolean) {
  try {
    katex.render(tex, dom, {
      displayMode,
      throwOnError: false,
      output: 'htmlAndMathml',
      strict: 'ignore',
    })
  } catch {
    dom.textContent = displayMode ? `$$${tex}$$` : `$${tex}$`
    dom.classList.add('math-error')
  }
}

// markdown-it inline rule for ``$...$``. Conservative — refuses spans
// that look like currency (``$5 + $3``) by requiring non-whitespace
// adjacency on both sides and rejecting a digit immediately after the
// closing ``$``. Mirrors the heuristics of markdown-it-texmath.
function inlineMathRule(state: MdState, silent: boolean): boolean {
  const src = state.src
  const start = state.pos
  if (src.charCodeAt(start) !== 0x24 /* $ */) return false
  const next = src.charCodeAt(start + 1)
  // Empty (``$$``), whitespace-led, or newline-led spans aren't inline.
  if (next === 0x24 || next === 0x20 || next === 0x09 || next === 0x0a || isNaN(next)) {
    return false
  }
  // Scan for the closing ``$`` on the same line, honouring ``\$`` escapes.
  let pos = start + 1
  let found = -1
  while (pos < src.length) {
    const c = src.charCodeAt(pos)
    if (c === 0x5c /* \ */) {
      pos += 2
      continue
    }
    if (c === 0x0a) return false
    if (c === 0x24) {
      found = pos
      break
    }
    pos += 1
  }
  if (found < 0) return false
  // Closing ``$`` must hug the content (no trailing whitespace) and
  // must not be followed by a digit (currency heuristic).
  const prev = src.charCodeAt(found - 1)
  if (prev === 0x20 || prev === 0x09) return false
  const afterClose = src.charCodeAt(found + 1)
  if (afterClose >= 0x30 && afterClose <= 0x39) return false
  if (!silent) {
    const token = state.push('math_inline', 'span', 0)
    token.markup = '$'
    token.content = src.slice(start + 1, found)
  }
  state.pos = found + 1
  return true
}

// markdown-it block rule for ``$$...$$``: opens on a line starting with
// ``$$`` and runs until a line ending with ``$$``. Single-line form
// (``$$ x^2 $$``) is supported too.
function blockMathRule(
  state: MdBlockState,
  startLine: number,
  endLine: number,
  silent: boolean,
): boolean {
  let pos = state.bMarks[startLine] + state.tShift[startLine]
  let max = state.eMarks[startLine]
  if (pos + 2 > max) return false
  if (state.src.slice(pos, pos + 2) !== '$$') return false
  const firstLine = state.src.slice(pos + 2, max)
  let lastLine = startLine
  let content = ''
  if (firstLine.trimEnd().endsWith('$$')) {
    content = firstLine.trimEnd().slice(0, -2).trim()
  } else {
    let found = false
    for (let line = startLine + 1; line < endLine; line++) {
      pos = state.bMarks[line] + state.tShift[line]
      max = state.eMarks[line]
      const text = state.src.slice(pos, max).trimEnd()
      if (text.endsWith('$$')) {
        lastLine = line
        const head = firstLine.trim()
        const tail = text.slice(0, -2).trimEnd()
        const middle: string[] = []
        for (let l = startLine + 1; l < line; l++) {
          middle.push(
            state.src.slice(state.bMarks[l] + state.tShift[l], state.eMarks[l]),
          )
        }
        const parts: string[] = []
        if (head) parts.push(head)
        if (middle.length) parts.push(middle.join('\n'))
        if (tail) parts.push(tail)
        content = parts.join('\n')
        found = true
        break
      }
    }
    if (!found) return false
  }
  if (silent) return true
  const token = state.push('math_block', 'div', 0)
  token.block = true
  token.markup = '$$'
  token.content = content
  state.line = lastLine + 1
  return true
}

// Idempotent registration: tiptap-markdown calls parse.setup once per
// parse, and re-registering a markdown-it rule with the same name
// throws. We tag the instance after the first installation.
type MdWithMathFlags = { __flowMathInline?: boolean; __flowMathBlock?: boolean }

function ensureInlineRule(md: MdInstance & MdWithMathFlags) {
  if (md.__flowMathInline) return
  md.inline.ruler.after('escape', 'math_inline', inlineMathRule)
  md.renderer.rules['math_inline'] = (tokens: MdToken[], idx: number) => {
    const tex = tokens[idx].content
    const safe = tex.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;')
    return `<span data-inline-math="${safe}"></span>`
  }
  md.__flowMathInline = true
}

function ensureBlockRule(md: MdInstance & MdWithMathFlags) {
  if (md.__flowMathBlock) return
  md.block.ruler.before('fence', 'math_block', blockMathRule, {
    alt: ['paragraph', 'reference', 'blockquote', 'list'],
  })
  md.renderer.rules['math_block'] = (tokens: MdToken[], idx: number) => {
    const tex = tokens[idx].content
    const safe = tex.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;')
    return `<div data-block-math="${safe}"></div>\n`
  }
  md.__flowMathBlock = true
}

// markdown-it types are not in the project; declare the minimal shape
// the rules touch (signatures + helpers).
type MdToken = { type: string; tag: string; content: string; block?: boolean; markup?: string }
type MdState = {
  src: string
  pos: number
  push: (type: string, tag: string, nesting: number) => MdToken
}
type MdBlockState = {
  src: string
  bMarks: number[]
  eMarks: number[]
  tShift: number[]
  line: number
  push: (type: string, tag: string, nesting: number) => MdToken
}
type MdInstance = {
  inline: { ruler: { after: (anchor: string, name: string, fn: (s: MdState, silent: boolean) => boolean) => void } }
  block: {
    ruler: {
      before: (
        anchor: string,
        name: string,
        fn: (s: MdBlockState, start: number, end: number, silent: boolean) => boolean,
        opts?: { alt?: string[] },
      ) => void
    }
  }
  renderer: { rules: Record<string, (tokens: MdToken[], idx: number) => string> }
}

// tiptap-markdown serializer signature is loose; type the touch points.
type MdSerializerState = { write: (s: string) => void; closeBlock: (node: unknown) => void }
type WithTexAttrs = { attrs: { tex?: string } }

export const InlineMath = Node.create({
  name: 'inlineMath',
  group: 'inline',
  inline: true,
  atom: true,
  selectable: true,
  draggable: false,

  addAttributes() {
    return {
      tex: {
        default: '',
        parseHTML: (el: HTMLElement) => el.getAttribute('data-inline-math') ?? '',
        renderHTML: (attrs: { tex?: string }) => ({
          'data-inline-math': attrs.tex ?? '',
        }),
      },
    }
  },

  parseHTML() {
    return [{ tag: 'span[data-inline-math]' }]
  },

  renderHTML({ HTMLAttributes }) {
    return ['span', mergeAttributes({ class: 'math-inline' }, HTMLAttributes)]
  },

  addNodeView() {
    return ({ node }: { node: WithTexAttrs }) => {
      const dom = document.createElement('span')
      dom.className = 'math-inline'
      const tex = node.attrs.tex ?? ''
      dom.setAttribute('data-inline-math', tex)
      dom.setAttribute('contenteditable', 'false')
      renderTexInto(dom, tex, false)
      return { dom }
    }
  },

  addInputRules() {
    // Fire when the user closes a ``$...$`` span. Same content rules as
    // the markdown-it tokenizer above: no whitespace adjacency, no ``$``
    // inside, content starts/ends on non-whitespace.
    return [
      nodeInputRule({
        find: /(?:^|[^\\\w$])\$([^\s$][^$\n]*?[^\s$])\$$/,
        type: this.type,
        getAttributes: (match) => ({ tex: match[1] }),
      }),
      // Single-char inline: $x$
      nodeInputRule({
        find: /(?:^|[^\\\w$])\$([^\s$])\$$/,
        type: this.type,
        getAttributes: (match) => ({ tex: match[1] }),
      }),
    ]
  },

  addStorage() {
    return {
      markdown: {
        serialize(state: MdSerializerState, node: WithTexAttrs) {
          state.write(`$${node.attrs.tex ?? ''}$`)
        },
        parse: {
          setup(md: MdInstance & MdWithMathFlags) {
            ensureInlineRule(md)
          },
        },
      },
    }
  },
})

export const BlockMath = Node.create({
  name: 'blockMath',
  group: 'block',
  atom: true,
  selectable: true,
  draggable: false,

  addAttributes() {
    return {
      tex: {
        default: '',
        parseHTML: (el: HTMLElement) => el.getAttribute('data-block-math') ?? '',
        renderHTML: (attrs: { tex?: string }) => ({
          'data-block-math': attrs.tex ?? '',
        }),
      },
    }
  },

  parseHTML() {
    return [{ tag: 'div[data-block-math]' }]
  },

  renderHTML({ HTMLAttributes }) {
    return ['div', mergeAttributes({ class: 'math-block' }, HTMLAttributes)]
  },

  addNodeView() {
    return ({ node }: { node: WithTexAttrs }) => {
      const dom = document.createElement('div')
      dom.className = 'math-block'
      const tex = node.attrs.tex ?? ''
      dom.setAttribute('data-block-math', tex)
      dom.setAttribute('contenteditable', 'false')
      renderTexInto(dom, tex, true)
      return { dom }
    }
  },

  addStorage() {
    return {
      markdown: {
        serialize(state: MdSerializerState, node: WithTexAttrs) {
          state.write(`$$\n${node.attrs.tex ?? ''}\n$$`)
          state.closeBlock(node)
        },
        parse: {
          setup(md: MdInstance & MdWithMathFlags) {
            ensureBlockRule(md)
          },
        },
      },
    }
  },
})
