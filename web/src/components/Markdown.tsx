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
import {
  fetchTaskMention,
  getCachedTaskMention,
  type TaskMentionInfo,
} from '../lib/taskMentionCache'

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
