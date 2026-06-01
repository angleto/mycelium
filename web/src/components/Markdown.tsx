import Markdown from 'react-markdown'
import type { Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import rehypeKatex from 'rehype-katex'
import { Link } from 'react-router-dom'
import { useEffect, useState, type ReactNode } from 'react'
import 'katex/dist/katex.min.css'
import { parseMentionHref, routeForMention, type MentionKind } from '../lib/mentions'
import { useAuthBlobUrl } from '../lib/useAuthBlobUrl'
import { isAttachmentHref, openAttachment } from '../lib/attachmentRef'
import {
  fetchTaskMention,
  getCachedTaskMention,
  type TaskMentionInfo,
} from '../lib/taskMentionCache'
import { isPrefixCandidate } from '../lib/prefixLookup'
import { PrefixMentionChip } from './PrefixMentionChip'

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

// Flatten a link's children to its plain text (the filename) so the
// click handler can name the download and pick inline-vs-download.
function nodeText(children: ReactNode): string | undefined {
  if (typeof children === 'string') return children
  if (Array.isArray(children)) {
    const s = children.map((c) => (typeof c === 'string' ? c : '')).join('')
    return s || undefined
  }
  return undefined
}

// A non-image attachment linked in the body: `[filename](/attachments/<id>/download)`.
// The route is bearer-authenticated, so this is NOT a plain navigation —
// the click authFetches the bytes and opens/downloads them via an
// ephemeral object URL (openAttachment). The `md-att` class both styles
// the link (leading icon) and tells the global editor click-interceptor
// to leave this one to React (no double handling).
function AttachmentLink({
  href,
  children,
}: {
  href: string
  children: ReactNode
}) {
  const name = nodeText(children)
  return (
    <a
      href={href}
      className="md-att"
      onClick={(e) => {
        e.preventDefault()
        void openAttachment(href, name)
      }}
    >
      {children}
    </a>
  )
}

// Task mention chip: looks the task up so the label can carry the
// task's current workflow state (in parens) and, when the state is
// terminal, render strikethrough-gray. The lookup is cached
// module-side so many references on one page collapse to one fetch.
function TaskMentionChip({
  id,
  kind,
  children,
}: {
  id: string
  kind: MentionKind
  children: ReactNode
}) {
  const [info, setInfo] = useState<TaskMentionInfo | null | undefined>(() =>
    getCachedTaskMention(id),
  )
  useEffect(() => {
    if (info !== undefined) return
    let alive = true
    void fetchTaskMention(id).then((res) => {
      if (alive) setInfo(res)
    })
    return () => {
      alive = false
    }
  }, [id, info])
  const closed = info?.isTerminal === true
  const cls =
    'chip' +
    (closed ? ' chip--task-closed' : '') +
    (info && !closed && info.stateName ? ' chip--task-open' : '')
  return (
    <Link className={cls} to={routeForMention(kind, id)} title={info?.title ?? undefined}>
      <span className="chip__label">{children}</span>
      {info?.stateName && (
        <span className="chip__state" aria-label={`state ${info.stateName}`}>
          {' '}
          ({info.stateName})
        </span>
      )}
    </Link>
  )
}

const components: Components = {
  a({ href, children }) {
    const m = href ? parseMentionHref(href) : null
    if (m) {
      if (m.kind === 'task') {
        return (
          <TaskMentionChip id={m.id} kind={m.kind}>
            {children}
          </TaskMentionChip>
        )
      }
      return (
        <Link className="chip" to={routeForMention(m.kind, m.id)}>
          {children}
        </Link>
      )
    }
    if (isAttachmentHref(href)) {
      return <AttachmentLink href={href}>{children}</AttachmentLink>
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
  // Inline ``code`` whose text matches a UUID prefix (4-36 hex, dashes
  // ok) is turned into a clickable mention chip. The convention in
  // roadmap notes is `91cf6aaa`-style backticked prefixes; without
  // this hook they render as dead literals. Block code (fenced /
  // language-tagged) is untouched because react-markdown passes a
  // ``className`` like ``language-foo``; we only intercept when there
  // is no className and children is a single string node.
  code({ className, children, ...rest }) {
    if (!className && typeof children === 'string' && isPrefixCandidate(children)) {
      return <PrefixMentionChip prefix={children} />
    }
    return (
      <code className={className} {...rest}>
        {children}
      </code>
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
