// ``<sup>`` / ``<sub>`` in the editor, and ONLY those two tags.
//
// The read side renders them via ``remarkSubSup``; without the matching
// pair here the editor would show a formula's exponents as raw tags, and
// the body would not be a fixed point of the markdown round-trip, so the
// whole part would fall back to source mode (see ``RichEditor``). Two
// marks and one DOM pass keep such notes editable in WYSIWYG.
//
// Deliberately NOT done by flipping tiptap-markdown to ``html: true``:
// that would hand every inline tag to markdown-it, and ProseMirror would
// then silently unwrap the ones its schema does not know -- turning
// ``<div>x</div>`` into ``x``. Instead markdown-it keeps escaping HTML as
// text, and ``parse.updateDOM`` promotes just these two tags back into
// elements, in text nodes only, never inside ``code``/``pre`` (where the
// author asked for the characters, not the markup). Anything else stays
// literal text exactly as before.

import { Mark } from '@tiptap/core'

/** ``<sup>x</sup>`` with no nested markup. The backreference forces the
 *  closing tag to match, and ``[^<>]*`` keeps the content tag-free, so the
 *  element we build below can never carry anything but plain text. */
const PAIR = /<(sup|sub)>([^<>]*)<\/\1>/g

function hasCodeAncestor(node: Node, root: Node): boolean {
  let el = node.parentElement
  while (el && el !== root) {
    const tag = el.tagName.toLowerCase()
    if (tag === 'code' || tag === 'pre') return true
    el = el.parentElement
  }
  return false
}

/** Promote the escaped ``<sup>``/``<sub>`` text back into real elements,
 *  in place, before ProseMirror parses the DOM. */
export function promoteSubSup(root: HTMLElement): void {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
  const targets: Text[] = []
  let n = walker.nextNode()
  while (n) {
    const text = n as Text
    PAIR.lastIndex = 0
    if (PAIR.test(text.data) && !hasCodeAncestor(text, root)) targets.push(text)
    n = walker.nextNode()
  }
  for (const text of targets) {
    const frag = document.createDocumentFragment()
    let last = 0
    PAIR.lastIndex = 0
    let m = PAIR.exec(text.data)
    while (m) {
      if (m.index > last) frag.append(text.data.slice(last, m.index))
      const el = document.createElement(m[1])
      el.textContent = m[2]
      frag.append(el)
      last = m.index + m[0].length
      m = PAIR.exec(text.data)
    }
    if (last < text.data.length) frag.append(text.data.slice(last))
    text.replaceWith(frag)
  }
}

function subSupMark(name: 'superscript' | 'subscript', tag: 'sup' | 'sub', first: boolean) {
  return Mark.create({
    name,
    // A character is one or the other, never both.
    excludes: 'superscript subscript',
    parseHTML() {
      return [{ tag }]
    },
    renderHTML() {
      return [tag, 0]
    },
    addStorage() {
      return {
        markdown: {
          serialize: {
            open: `<${tag}>`,
            close: `</${tag}>`,
            // The tags are inline markup around the text, so they must
            // hug it: no whitespace pushed inside the pair.
            expelEnclosingWhitespace: true,
          },
          // markdown-it escapes the tags to text (html mode stays off);
          // the DOM pass above turns them back into elements. Registered
          // once, on the first of the two marks.
          parse: first ? { updateDOM: promoteSubSup } : {},
        },
      }
    },
  })
}

export const Superscript = subSupMark('superscript', 'sup', true)
export const Subscript = subSupMark('subscript', 'sub', false)
