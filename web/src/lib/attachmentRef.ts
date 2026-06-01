import { authFetch } from '../api/client'

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

// Types a browser can render inline (a new-tab blob view). Anything else
// is downloaded instead. We key on the filename extension because the
// markdown link only carries the name, not the mime.
const PREVIEWABLE_EXT = /\.(pdf|png|jpe?g|gif|webp|svg|bmp)$/i

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
