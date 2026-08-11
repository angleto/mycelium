// Render ``<sup>`` and ``<sub>`` inline, and ONLY those two tags.
//
// Superscripts and subscripts are the cheapest notation for light maths in
// prose (``x<sub>0</sub>``, ``2<sup>t</sup>``), they are what GitHub
// supports, and CommonMark has no markup for them. Until now they reached
// the renderer as raw ``html`` mdast nodes, which react-markdown drops
// because the pipeline runs without ``allowDangerousHtml`` -- the tags
// vanished and only the digit was left, silently changing the meaning of a
// formula.
//
// The obvious fix, ``rehype-raw`` + ``rehype-sanitize``, enables the whole
// HTML surface and then tries to take most of it back with an allow-list.
// This plugin does the opposite: it works on the mdast, matches the exact
// token sequence [html ``<sup>``, phrasing…, html ``</sup>``] and replaces
// it with a real element node. Every other raw-HTML node keeps today's
// behaviour (dropped). No attributes are ever carried over, the children
// are mdast nodes that were already parsed as markdown, and no HTML string
// is ever handed to the renderer, so this adds no injection surface.
//
// The element is emitted as an ``emphasis`` node carrying ``data.hName``,
// the documented mdast-util-to-hast override for "render this node as that
// tag". That keeps the tree valid mdast (phrasing content in a phrasing
// position) without teaching every downstream plugin a new node type.

type Node = {
  type: string
  value?: string
  children?: Node[]
  data?: { hName?: string }
}

const OPEN = /^<(sup|sub)>$/i
const CLOSE = /^<\/(sup|sub)>$/i

/** ``<sup>`` -> ``sup``; null when the node is not one of our open tags. */
function openTag(node: Node): string | null {
  if (node.type !== 'html' || typeof node.value !== 'string') return null
  const m = OPEN.exec(node.value.trim())
  return m ? m[1].toLowerCase() : null
}

function closesTag(node: Node, tag: string): boolean {
  if (node.type !== 'html' || typeof node.value !== 'string') return false
  const m = CLOSE.exec(node.value.trim())
  return !!m && m[1].toLowerCase() === tag
}

/** Rewrite one children array in place-ish, returning the new array. */
function rewrite(children: Node[]): Node[] {
  const out: Node[] = []
  for (let i = 0; i < children.length; i += 1) {
    const node = children[i]
    const tag = openTag(node)
    if (tag) {
      // Find the matching close. Nesting the same tag inside itself is not
      // a thing anyone writes, so the first close wins.
      const end = children.findIndex((c, j) => j > i && closesTag(c, tag))
      if (end !== -1) {
        out.push({
          type: 'emphasis',
          data: { hName: tag },
          children: rewrite(children.slice(i + 1, end)),
        })
        i = end
        continue
      }
      // Unbalanced: leave the raw node alone, i.e. dropped as before.
    }
    if (node.children) node.children = rewrite(node.children)
    out.push(node)
  }
  return out
}

export function remarkSubSup() {
  return (tree: Node): void => {
    if (tree.children) tree.children = rewrite(tree.children)
  }
}
