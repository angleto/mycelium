// Building the panel without a framework.
//
// The package ships no runtime dependency at all, which is what keeps a
// popup that has to paint before a keystroke feels slow from carrying a
// renderer, a router and their transitive tree. The surface is a list and
// a few controls; these six helpers are the whole abstraction.
//
// `el` sets TEXT, never markup. There is no innerHTML anywhere in this
// package, and that is deliberate rather than incidental: task titles and
// search snippets are content from the workspace, and a snippet arrives
// with the server's own <b> delimiters around text that was never
// escaped. Rendering it as markup would execute whatever is in a note.

type Attrs = Record<string, string | number | boolean | undefined>

export function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  attrs: Attrs = {},
  children: (Node | string)[] = [],
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag)
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === false) continue
    if (key === 'class') node.className = String(value)
    else if (key === 'text') node.textContent = String(value)
    else node.setAttribute(key, value === true ? '' : String(value))
  }
  for (const child of children) {
    node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child)
  }
  return node
}

export function clear(node: Element): void {
  node.replaceChildren()
}

/** A server snippet, rendered as TEXT with the matched runs marked.
 *
 *  The extract arrives with literal <b> and </b> around the match and the
 *  surrounding text NOT escaped, so a note containing `<img onerror=...>`
 *  arrives verbatim. Scanning for the two delimiters and emitting text
 *  nodes for everything between them means that image tag renders as the
 *  characters it is. A unit test feeds it exactly that. */
export function headline(snippet: string): DocumentFragment {
  const frag = document.createDocumentFragment()
  let rest = snippet
  while (rest.length > 0) {
    const open = rest.indexOf('<b>')
    if (open === -1) break
    const close = rest.indexOf('</b>', open)
    if (close === -1) break
    if (open > 0) frag.appendChild(document.createTextNode(rest.slice(0, open)))
    frag.appendChild(el('mark', { class: 'hypha__hl', text: rest.slice(open + 3, close) }))
    rest = rest.slice(close + 4)
  }
  // Whatever is left, including the whole string when the delimiters were
  // unbalanced. Degrading to plain text is right: the alternative is
  // swallowing the remainder of somebody's note.
  if (rest.length > 0) frag.appendChild(document.createTextNode(rest))
  return frag
}

export function on<K extends keyof HTMLElementEventMap>(
  node: EventTarget,
  type: K,
  handler: (event: HTMLElementEventMap[K]) => void,
): void {
  node.addEventListener(type, handler as EventListener)
}
