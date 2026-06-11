import { authFetch } from '../api/client'
import type { ImageUploadParent } from './imageUpload'
import {
  classifyAttachmentRef,
  ensureAttachmentManifest,
  resolveAttachmentName,
} from './attachmentManifest'

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

// Matches the canonical attachment download path emitted by the upload
// helpers, tolerating an optional query/hash. Keyed on the same prefix
// useAuthBlobUrl uses for images, so the two stay in lockstep.
const ATTACHMENT_HREF_RE = /^\/attachments\/[^/]+\/download(?:[?#]|$)/

export function isAttachmentHref(href: string | null | undefined): href is string {
  return !!href && ATTACHMENT_HREF_RE.test(href)
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
