import Markdown from 'react-markdown'
import type { Components } from 'react-markdown'
import remarkMath from 'remark-math'
import rehypeKatex from 'rehype-katex'
import { Link } from 'react-router-dom'
import 'katex/dist/katex.min.css'
import { parseMentionHref, routeForMention } from '../lib/mentions'

// Read-side markdown — the only renderer in the codebase, used by
// /notes turns, /garden plant detail, etc. Links whose href is the
// @kind:id DSL become an in-app router link rendered as a chip; every
// other link is a normal external anchor. LaTeX is supported via
// remark-math + rehype-katex: ``$inline$``, ``$$block$$``, and the
// `math` / `inlineMath` AST nodes from remark.
const components: Components = {
  a({ href, children }) {
    const m = href ? parseMentionHref(href) : null
    if (m) {
      return (
        <Link className="chip" to={routeForMention(m.kind, m.id)}>
          {children}
        </Link>
      )
    }
    return (
      <a href={href} target="_blank" rel="noopener noreferrer">
        {children}
      </a>
    )
  },
}

export function MarkdownView({ text }: { text: string }) {
  if (!text.trim()) return null
  return (
    <div className="md">
      <Markdown
        components={components}
        remarkPlugins={[remarkMath]}
        rehypePlugins={[rehypeKatex]}
      >
        {text}
      </Markdown>
    </div>
  )
}
