import Markdown from 'react-markdown'
import type { Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import rehypeKatex from 'rehype-katex'
import { Link } from 'react-router-dom'
import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import 'katex/dist/katex.min.css'
import { parseMentionHref, routeForMention, type MentionKind } from '../lib/mentions'
import { useAttachmentMedia } from '../lib/useAuthBlobUrl'
import { attachmentKind, isMarkdown } from '../lib/attachmentKind'
import { isAttachmentRef, openAttachmentByRef } from '../lib/attachmentRef'
import type { ImageUploadParent } from '../lib/imageUpload'
import {
  fetchTaskMention,
  getCachedTaskMention,
  type TaskMentionInfo,
} from '../lib/taskMentionCache'
import { isPrefixCandidate } from '../lib/prefixLookup'
import { remarkSubSup } from '../lib/remarkSubSup'
import { PrefixMentionChip } from './PrefixMentionChip'
import { Mermaid } from './Mermaid'

// A fenced ```mermaid block reaches react-markdown as <pre><code
// class="language-mermaid">; detect it from the class list (string or
// the hast array form) so it can render as a diagram instead of literal
// code.
function isMermaidClass(className: unknown): boolean {
  if (typeof className === 'string') return /\blanguage-mermaid\b/.test(className)
  if (Array.isArray(className)) return className.includes('language-mermaid')
  return false
}

// Flatten a code node's children to its raw source text.
function codeText(children: ReactNode): string {
  if (typeof children === 'string') return children
  if (Array.isArray(children)) {
    return children.map((c) => (typeof c === 'string' ? c : '')).join('')
  }
  return ''
}

// Read-side markdown — the only renderer in the codebase, used by
// /notes turns, /garden plant detail, etc. Links whose href is the
// @kind:id DSL become an in-app router link rendered as a chip; every
// other link is a normal external anchor. LaTeX is supported via
// remark-math + rehype-katex: ``$inline$``, ``$$block$$``, and the
// `math` / `inlineMath` AST nodes from remark.
//
// A `![]()` reference embeds the referenced attachment inline. The editor
// inserts `![filename](/attachments/<id>/download)` for images
// dropped/pasted/picked, and an author may also write `![alt](file.ext)`
// referencing any file uploaded to the same note/task. The attachment
// route is bearer-authenticated, so a naked <img src> would 401 (and a
// bare filename would 404); useAttachmentMedia resolves the reference
// against the parent's attachments and routes it through authFetch into a
// one-shot object URL, then we dispatch on the blob's kind:
//   image -> <img> (in a <figure> with a caption when a markdown title is
//            present: `![alt](src "the caption")`)
//   audio -> <audio controls>
//   video -> <video controls>
//   text  -> the contents inline (a `.md` rendered as markdown, anything
//            else as raw monospace), capped with a truncation note
//   pdf / other -> a clickable attachment link (a broken <img> is useless)
// Non-attachment URLs (http(s), data:, blob:) pass through. An
// unresolvable reference shows a broken-image placeholder, not a spinner.

// Max characters rendered for an embedded text file; the rest is a
// Download away. 256 KiB is plenty for a note-sized preview and keeps a
// stray multi-MB log from freezing the layout.
const TEXT_EMBED_MAX_CHARS = 256 * 1024

// How deep a `.md` attachment may embed another `.md` before we stop
// recursing and render it as raw text — guards an accidental cycle.
const MAX_MD_EMBED_DEPTH = 2

function AuthMedia({
  src,
  alt,
  title,
  parent,
  depth,
}: {
  src: string | undefined
  alt: string | undefined
  title: string | undefined
  parent?: ImageUploadParent
  depth: number
}) {
  const { url, mime, name, loading } = useAttachmentMedia(src, parent)
  // Classify by the FILE reference (a bare filename, or an absolute URL's
  // own extension), never the alt text — alt is a caption, not a name. The
  // blob mime wins when specific; the name breaks octet-stream ties.
  const classifyRef = name ?? src ?? undefined
  const kind = attachmentKind(mime, classifyRef)
  const displayName = name ?? alt ?? undefined

  if (loading) {
    return <span className="md-img md-img--loading" aria-label={alt ?? ''} />
  }
  if (!url || !src) {
    return (
      <span className="md-img md-img--broken" role="img" aria-label={alt ?? ''}>
        {alt || src || '?'}
      </span>
    )
  }

  const caption = title ? (
    <figcaption className="md-figcaption">{title}</figcaption>
  ) : null

  if (kind === 'audio') {
    return (
      <figure className="md-media md-media--audio">
        <audio src={url} controls preload="metadata" />
        {caption}
      </figure>
    )
  }
  if (kind === 'video') {
    return (
      <figure className="md-media md-media--video">
        <video src={url} controls preload="metadata" />
        {caption}
      </figure>
    )
  }
  if (kind === 'text') {
    return (
      <TextEmbed
        url={url}
        name={displayName}
        title={title}
        markdown={isMarkdown(mime, classifyRef)}
        parent={parent}
        depth={depth}
      />
    )
  }
  if (kind === 'pdf' || kind === 'other') {
    // A non-image file referenced with image syntax: a clickable link
    // beats a broken <img>. Resolves + auth-fetches like any md-att link.
    return (
      <a
        href={src}
        className="md-att"
        onClick={(e) => {
          e.preventDefault()
          void openAttachmentByRef(src, parent, displayName)
        }}
      >
        {displayName ?? src}
      </a>
    )
  }
  // image (and absolute-URL passthrough): bare <img>, or wrapped in a
  // <figure> when a caption was given.
  if (!caption) {
    return <img src={url} alt={alt ?? ''} title={title} className="md-img" />
  }
  return (
    <figure className="md-figure">
      <img src={url} alt={alt ?? ''} className="md-img" />
      {caption}
    </figure>
  )
}

// Inline preview of a text attachment. The bytes are already in the
// browser as `url` (a blob: object URL), so reading the text is local — no
// network, no auth. A `.md` is rendered through MarkdownView (bounded by
// MAX_MD_EMBED_DEPTH); anything else is shown verbatim in a monospace
// block, truncated to TEXT_EMBED_MAX_CHARS.
function TextEmbed({
  url,
  name,
  title,
  markdown,
  parent,
  depth,
}: {
  url: string
  name: string | undefined
  title: string | undefined
  markdown: boolean
  parent?: ImageUploadParent
  depth: number
}) {
  const { t } = useTranslation()
  // State is tagged with the `url` it was fetched for, so a `url` change
  // reads as "loading" by derivation — no synchronous setState reset in the
  // effect (which would trigger a cascading render).
  const [state, setState] = useState<
    | { for: string; phase: 'error' }
    | { for: string; phase: 'ready'; text: string; truncated: boolean }
    | null
  >(null)

  useEffect(() => {
    let active = true
    void (async () => {
      try {
        const res = await fetch(url)
        const raw = await res.text()
        if (!active) return
        const truncated = raw.length > TEXT_EMBED_MAX_CHARS
        setState({
          for: url,
          phase: 'ready',
          text: truncated ? raw.slice(0, TEXT_EMBED_MAX_CHARS) : raw,
          truncated,
        })
      } catch {
        if (active) setState({ for: url, phase: 'error' })
      }
    })()
    return () => {
      active = false
    }
  }, [url])

  const current = state && state.for === url ? state : null
  if (!current) {
    return <span className="md-img md-img--loading" aria-label={name ?? ''} />
  }
  if (current.phase === 'error') {
    return (
      <span className="md-img md-img--broken" role="img" aria-label={name ?? ''}>
        {name || '?'}
      </span>
    )
  }

  const renderMarkdown = markdown && depth < MAX_MD_EMBED_DEPTH
  return (
    <figure className="md-media md-media--text">
      {name && <span className="md-textfile__name">{name}</span>}
      {renderMarkdown ? (
        <div className="md-textfile__md">
          <MarkdownView text={current.text} parent={parent} depth={depth + 1} />
        </div>
      ) : (
        <pre className="md-textfile">
          <code>{current.text}</code>
        </pre>
      )}
      {current.truncated && (
        <p className="hint md-textfile__more">{t('attach.textTruncated')}</p>
      )}
      {title && <figcaption className="md-figcaption">{title}</figcaption>}
    </figure>
  )
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

// A non-image attachment linked in the body, either by its canonical
// `[filename](/attachments/<id>/download)` href or by a bare filename
// `[label](report.pdf)` referencing a file uploaded to the same
// note/task. The route is bearer-authenticated, so this is NOT a plain
// navigation — the click resolves the ref (filename → id via the
// parent's manifest when needed) then authFetches the bytes and
// opens/downloads them via an ephemeral object URL. The `md-att` class
// both styles the link (leading icon) and tells the global editor
// click-interceptor to leave this one to React (no double handling).
function AttachmentLink({
  href,
  parent,
  children,
}: {
  href: string
  parent?: ImageUploadParent
  children: ReactNode
}) {
  const name = nodeText(children)
  return (
    <a
      href={href}
      className="md-att"
      onClick={(e) => {
        e.preventDefault()
        void openAttachmentByRef(href, parent, name)
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

// A paragraph whose only meaningful child is a single image reference. Such
// a `![]()` on its own line may render as a BLOCK media embed (an
// audio/video/text <figure>), which must not be nested inside a <p>
// (invalid HTML; React warns). Detected from the source node so the <p>
// can be unwrapped before the async kind is even known.
function isLoneImageParagraph(node: unknown): boolean {
  const children =
    node && typeof node === 'object' && 'children' in node
      ? (node as { children?: Array<{ type?: string; tagName?: string; value?: string }> })
          .children ?? []
      : []
  const meaningful = children.filter(
    (c) => c.type !== 'text' || (c.value ?? '').trim() !== '',
  )
  return (
    meaningful.length === 1 &&
    meaningful[0].type === 'element' &&
    meaningful[0].tagName === 'img'
  )
}

function makeComponents(parent?: ImageUploadParent, depth = 0): Components {
  return {
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
    if (isAttachmentRef(href, parent)) {
      return (
        <AttachmentLink href={href} parent={parent}>
          {children}
        </AttachmentLink>
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
      <AuthMedia
        src={strOrUndef(src)}
        alt={strOrUndef(alt)}
        title={strOrUndef(title)}
        parent={parent}
        depth={depth}
      />
    )
  },
  // Unwrap a paragraph that is just a lone image reference so a block media
  // embed (figure/pre/div) sits in a clean block context instead of an
  // invalid <p>. Paragraphs with images interleaved in text keep their <p>
  // (those images render inline as before).
  p({ node, children }) {
    if (isLoneImageParagraph(node)) {
      return <div className="md-embed">{children}</div>
    }
    return <p>{children}</p>
  },
  // Unwrap the <pre> around a ```mermaid block so the diagram renders on
  // its own — a block-level <div>/<svg> can't be nested inside <pre>, and
  // the <pre> chrome (mono background, padding) would frame the graph.
  // Every other code block keeps its normal <pre> wrapper.
  pre({ node, children, ...rest }) {
    const child = node?.children?.[0]
    const cls =
      child && child.type === 'element' ? child.properties?.className : undefined
    if (isMermaidClass(cls)) return <>{children}</>
    return <pre {...rest}>{children}</pre>
  },
  // A fenced ```mermaid block renders as a diagram. Inline ``code`` whose
  // text matches a UUID prefix (4-36 hex, dashes ok) is turned into a
  // clickable mention chip (the `91cf6aaa`-style roadmap convention).
  // Other block code (fenced / language-tagged) is untouched because
  // react-markdown passes a ``className`` like ``language-foo``; the
  // prefix hook only intercepts when there is no className.
  code({ className, children, ...rest }) {
    if (isMermaidClass(className)) {
      return <Mermaid code={codeText(children).replace(/\n$/, '')} />
    }
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
}

export function MarkdownView({
  text,
  parent,
  depth = 0,
}: {
  text: string
  // Owning note/task, when known: lets `![alt](filename.png)` references
  // resolve against that parent's attachments. Omit where there is no
  // single owning entity.
  parent?: ImageUploadParent
  // Recursion depth when an embedded `.md` attachment is itself rendered
  // as markdown. Top-level callers leave this at 0; the embed bumps it so
  // MAX_MD_EMBED_DEPTH can break a cycle.
  depth?: number
}) {
  const components = useMemo(
    () => makeComponents(parent, depth),
    // parent depended on via kind/id (a fresh object each render).
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [parent?.kind, parent?.id, depth],
  )
  if (!text.trim()) return null
  return (
    <div className="md">
      <Markdown
        components={components}
        remarkPlugins={[remarkGfm, remarkMath, remarkSubSup]}
        rehypePlugins={[rehypeKatex]}
      >
        {text}
      </Markdown>
    </div>
  )
}
