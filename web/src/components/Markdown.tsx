import Markdown from 'react-markdown'
import type { Components } from 'react-markdown'
import { Link } from 'react-router-dom'
import { parseMentionHref, routeForMention } from '../lib/mentions'

// Read-side markdown. Links whose href is the @kind:id DSL become an
// in-app router link rendered as a chip (resolved reference); every
// other link is a normal external anchor.
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
      <Markdown components={components}>{text}</Markdown>
    </div>
  )
}
