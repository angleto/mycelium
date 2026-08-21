import { authFetch } from '../api/client'
import type { ImageUploadParent } from './imageUpload'
import { attachmentKind } from './attachmentKind'
import {
  classifyAttachmentRef,
  ensureAttachmentManifest,
  resolveAttachmentName,
} from './attachmentManifest'
import { mdLink, mdUnescapeLabel } from './markdownInline'

// Markdown references to attachments.
//
// An attachment linked in a note/task body is stored as ordinary
// markdown pointing at the SAME bearer-authenticated route the
// Attachments panel and embedded images already use:
//
//   image  -> ![filename](/attachments/<id>/download)
//   file   -> [filename](/attachments/<id>/download)
//
// The route is NEVER public: a bare <a href> / <img src> to it 401s
// because no Authorization header rides along. Every reference is
// resolved through authFetch into an ephemeral in-browser object URL
// (images via useAuthBlobUrl; file links via openAttachment below).
// There are no signed/tokenised/public URLs anywhere — an attachment is
// only ever reachable by an authenticated session.

// The canonical download path lives here ONCE: the matcher below and the
// paste-ready reference emitted by attachmentMarkdownRef are both built
// from these two literals, so the route cannot be changed on the emitting
// side without the matcher following. A drift there is silent and nasty —
// every already-written body would keep its `/attachments/...` href while
// isAttachmentHref stopped recognising it, turning live attachments into
// dead links that 401 in a new tab. Same prefix useAuthBlobUrl keys on for
// images, so those stay in lockstep too.
const ATTACHMENT_PATH_PREFIX = '/attachments/'
const ATTACHMENT_PATH_SUFFIX = '/download'

// Matches the canonical attachment download path emitted by the upload
// helpers, tolerating an optional query/hash. Neither literal contains a
// regex metacharacter, so they interpolate verbatim.
const ATTACHMENT_HREF_RE = new RegExp(
  `^${ATTACHMENT_PATH_PREFIX}[^/]+${ATTACHMENT_PATH_SUFFIX}(?:[?#]|$)`,
)

function attachmentDownloadPath(id: string): string {
  return `${ATTACHMENT_PATH_PREFIX}${id}${ATTACHMENT_PATH_SUFFIX}`
}

export function isAttachmentHref(href: string | null | undefined): href is string {
  return !!href && ATTACHMENT_HREF_RE.test(href)
}

// A reference to an attachment, decomposed: whether it embeds (`!`), the
// label between the brackets, and the canonical href. Both directions go
// through this shape — attachmentRefFor() builds one from attachment
// metadata, parseAttachmentMarkdownRef() recovers one from a pasted
// string — so the markdown emitters and the editor's node insertion share
// a single model instead of each re-deriving it.
export type AttachmentRef = {
  /** Render as `![...]` (inline embed) rather than `[...]` (link). */
  image: boolean
  /** Never empty: a nameless attachment falls back to REF_FALLBACK_NAME. */
  label: string
  /** Canonical `/attachments/<id>/download` path. */
  href: string
}

// Label for a nameless attachment: an empty one would give an invisible
// `[](…)` link, and the editor's node insertion would throw outright (an
// empty ProseMirror text node is illegal). Defensive in practice — the
// backend's filename sanitiser already falls back to this same word, so a
// stored attachment is never nameless — and the same fallback the CLI
// helper (`attachment_markdown_ref`, cli/cmds/_common.py) applies.
const REF_FALLBACK_NAME = 'file'

/**
 * The reference to an attachment, as its parts. The image test goes
 * through attachmentKind() rather than a bare `mime.startsWith('image/')`
 * so that this and the Attachments panel (which picks its preview
 * affordance the same way) never disagree about a row: a `.png` the
 * server could only type as octet-stream is shown with a thumbnail there,
 * and must embed rather than link here.
 */
export function attachmentRefFor(att: {
  id: string
  filename?: string | null
  mime_type?: string | null
}): AttachmentRef {
  return {
    image: attachmentKind(att.mime_type, att.filename) === 'image',
    label: att.filename || REF_FALLBACK_NAME,
    href: attachmentDownloadPath(att.id),
  }
}

/**
 * The paste-ready markdown reference to an attachment, and the only place
 * the web builds that string:
 *
 *   image  -> ![filename](/attachments/<id>/download)
 *   other  -> [filename](/attachments/<id>/download)
 *
 * Same shape and same bearer-auth route the MCP tool emits (`attach_file`'s
 * `markdown_ref`, mcp/src/mycelium_mcp/server.py) and the CLI helper
 * (`attachment_markdown_ref`, cli/src/mycelium_cli/cmds/_common.py), so a
 * body written from the web, from the CLI or by an agent renders the same.
 *
 * The image predicate is deliberately NOT identical to theirs: both test
 * `mime_type.startswith("image/")` on the stored (lowercased) mime, while
 * attachmentRefFor goes through attachmentKind(), which also treats a file
 * the backend could only type as `application/octet-stream` but whose
 * extension is an image one (`.png`, `.heic`, …) as an image. Whenever the
 * stored mime is a specific `image/*` type the three build the identical
 * string for the identical attachment; on that octet-stream tail the web
 * emits `![…]` where the MCP tool and the CLI emit `[…]`. That is the
 * intended asymmetry: only the web has a panel showing a thumbnail for the
 * very same row, and a link there next to a thumbnail here would be the
 * inconsistency users actually notice.
 */
export function attachmentMarkdownRef(att: {
  id: string
  filename?: string | null
  mime_type?: string | null
}): string {
  const { image, label, href } = attachmentRefFor(att)
  // The label is a filename, i.e. user data: `Report ]final.pdf` used to
  // emit a string that is not a link at all. mdLink escapes both halves.
  return mdLink(label, href, { image })
}

// One whole markdown link/image on a single line, with a
// parenthesis-free, space-free destination. The destination is then
// checked against isAttachmentHref, so only the canonical
// /attachments/<id>/download route is ever accepted.
//
// The label admits BACKSLASH ESCAPES (`\]`, `\[`, `\\`), because that is
// what attachmentMarkdownRef now emits for a filename containing a
// bracket. A label class of `[^\]\n]*` would stop matching those exact
// references -- the ones this matcher exists to recognise.
const MARKDOWN_REF_RE = /^(!?)\[((?:\\[\s\S]|[^\\\]\n])*)\]\(([^\s()]+)\)$/

/**
 * Recover an AttachmentRef from a pasted markdown reference — the exact
 * string attachmentMarkdownRef, the MCP `attach_file` tool and the CLI
 * hand a user to paste. Returns null unless the WHOLE input (surrounding
 * whitespace aside) is one such reference pointing at the canonical
 * download route, so any other paste is left to its normal handling.
 *
 * The WYSIWYG editor needs this because ProseMirror pastes plain text as
 * plain text: the reference would land as a literal text node and
 * prosemirror-markdown escapes `[` and `]` on the way back out, so the
 * saved body would hold `!\[name\](/attachments/…)` and readers would see
 * those characters instead of the image or the link.
 */
export function parseAttachmentMarkdownRef(text: string): AttachmentRef | null {
  const m = MARKDOWN_REF_RE.exec(text.trim())
  if (!m) return null
  const href = m[3]
  if (!isAttachmentHref(href)) return null
  const label = mdUnescapeLabel(m[2])
  return { image: m[1] === '!', label: label || REF_FALLBACK_NAME, href }
}

// Extensions a browser can SAFELY render when an attachment link is opened
// in a new tab. The tab navigates to a blob: object URL, which inherits the
// app's origin — so an executable type (html, svg, xhtml) is deliberately
// EXCLUDED to avoid stored-XSS, and downloaded instead. The backend's
// inline-safe Content-Disposition allowlist mirrors this set. We key on the
// filename extension because the markdown link only carries the name.
const PREVIEWABLE_EXT =
  /\.(pdf|png|jpe?g|jfif|gif|webp|bmp|ico|tiff?|avif|heic|heif|apng|mp3|wav|ogg|oga|opus|m4a|aac|flac|weba|mp4|m4v|webm|mov|ogv|txt|text|log|md|markdown|csv|tsv|json|yaml|yml|toml|ini)$/i

function isPreviewable(name: string | undefined): boolean {
  return !!name && PREVIEWABLE_EXT.test(name)
}

function triggerDownload(url: string, filename: string | undefined): void {
  const a = document.createElement('a')
  a.href = url
  if (filename) a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
}

/**
 * Open an attachment referenced by its markdown href
 * (`/attachments/<id>/download`). The route is bearer-authenticated, so
 * we authFetch the bytes (Authorization header attached by authFetch),
 * wrap them in a one-shot object URL, and either open them inline
 * (PDF/image, in a new tab) or download them (anything the browser
 * cannot preview). Nothing is exposed without the session token: the
 * object URL lives only in this tab and is revoked shortly after.
 *
 * Popup blockers require window.open to run inside the click gesture,
 * before the await — so the preview tab is opened synchronously and
 * navigated once the blob is ready.
 */
/**
 * Whether a markdown link href should be handled as an attachment in the
 * context of `parent`: either the canonical /attachments route, or a
 * bare filename (resolved against the parent's files on click). Used to
 * decide styling/handling at render time; the actual id lookup happens in
 * openAttachmentByRef, which can await the manifest.
 */
export function isAttachmentRef(
  href: string | null | undefined,
  parent?: ImageUploadParent,
): href is string {
  if (!href) return false
  if (isAttachmentHref(href)) return true
  return !!parent && classifyAttachmentRef(href) === 'name'
}

/**
 * Open an attachment referenced either by its canonical
 * /attachments/<id>/download href or by a bare filename. A filename is
 * resolved against `parent`'s attachment manifest (fetched on demand)
 * before opening; an unknown filename is a no-op (the caller has already
 * suppressed the default navigation). Delegates to openAttachment for the
 * actual bearer-auth fetch + inline-open/download.
 */
export async function openAttachmentByRef(
  rawHref: string,
  parent: ImageUploadParent | undefined,
  filename?: string,
): Promise<void> {
  if (isAttachmentHref(rawHref)) {
    await openAttachment(rawHref, filename)
    return
  }
  if (!parent || classifyAttachmentRef(rawHref) !== 'name') return
  let resolved = resolveAttachmentName(parent, rawHref)
  if (!resolved) {
    await ensureAttachmentManifest(parent)
    resolved = resolveAttachmentName(parent, rawHref)
  }
  if (resolved) await openAttachment(resolved, filename ?? rawHref)
}

export async function openAttachment(
  href: string,
  filename?: string,
): Promise<void> {
  const preview = isPreviewable(filename)
  const tab = preview ? window.open('', '_blank') : null
  try {
    const res = await authFetch(href)
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    // blob() carries the response Content-Type, so the browser renders a
    // PDF/image inline and falls back to a download for opaque types.
    const url = URL.createObjectURL(await res.blob())
    if (tab) {
      tab.location.href = url
    } else {
      triggerDownload(url, filename)
    }
    // Defer revoke: revoking immediately cancels the in-flight tab load
    // or download (same lifetime the Attachments panel uses).
    window.setTimeout(() => URL.revokeObjectURL(url), 60_000)
  } catch (e) {
    tab?.close()
    throw e
  }
}
