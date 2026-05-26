import Markdown from 'react-markdown'
import type { Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import rehypeKatex from 'rehype-katex'
import { Link } from 'react-router-dom'
import 'katex/dist/katex.min.css'
import { parseMentionHref, routeForMention } from '../lib/mentions'
import { useAuthBlobUrl } from '../lib/useAuthBlobUrl'

// Read-side markdown — the only renderer in the codebase, used by
// /notes turns, /garden plant detail, etc. Links whose href is the
// @kind:id DSL become an in-app router link rendered as a chip; every
// other link is a normal external anchor. LaTeX is supported via
// remark-math + rehype-katex: ``$inline$``, ``$$block$$``, and the
// `math` / `inlineMath` AST nodes from remark.
//
// Embedded images: the editor inserts `![filename](/attachments/<id>/download)`
// for files uploaded via drop/paste/picker. The attachment route is
// bearer-authenticated, so a naked <img src> would 401; AuthImg routes
// the src through useAuthBlobUrl so the browser sees a one-shot object
// URL. Non-attachment URLs (http(s), data:, blob:) pass through.
function AuthImg({
  src,
  alt,
  title,
}: {
  src: string | undefined
  alt: string | undefined
  title: string | undefined
}) {
  const resolved = useAuthBlobUrl(src)
  if (!resolved) {
    return <span className="md-img md-img--loading" aria-label={alt ?? ''} />
  }
  return <img src={resolved} alt={alt ?? ''} title={title} className="md-img" />
}

function strOrUndef(v: unknown): string | undefined {
  return typeof v === 'string' ? v : undefined
}

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
  img({ src, alt, title }) {
    return (
      <AuthImg
        src={strOrUndef(src)}
        alt={strOrUndef(alt)}
        title={strOrUndef(title)}
      />
    )
  },
}

export function MarkdownView({ text }: { text: string }) {
  if (!text.trim()) return null
  return (
    <div className="md">
      <Markdown
        components={components}
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={[rehypeKatex]}
      >
        {text}
      </Markdown>
    </div>
  )
}
