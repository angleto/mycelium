// Hand the browser a file this app built in memory.
//
// Extracted from RichEditor, which held the SPA's only text-download
// helper, so that a second caller does not become a seventh private copy
// of the Blob/objectURL dance (the existing copies already revoke on
// three different schedules, which is how you can tell nobody owns it).
//
// In-memory content only. Downloads that stream an authenticated
// response keep their own fetch handling: they have to read the filename
// out of ``Content-Disposition`` and may hand the object URL to
// ``window.open``, which needs a far later revoke than this one.

/** Strip what is unsafe or awkward in a filename across macOS / Windows
 *  / Linux, control characters included -- a newline reaching the
 *  ``download`` attribute is a real hazard, not a cosmetic one. Runs of
 *  whitespace collapse, the result is capped at 120 characters, and
 *  ``fallback`` stands in when nothing usable survives. */
export function sanitizeFilename(name: string | undefined, fallback = 'untitled'): string {
  const s = (name ?? '').trim()
  if (!s) return fallback
  const FORBIDDEN = /[\\/:*?"<>|]|\p{Cc}/gu
  const cleaned = s.replace(FORBIDDEN, '-').replace(/\s+/g, ' ').trim()
  return (cleaned || fallback).slice(0, 120)
}

/** Save ``content`` as ``filename``. The object URL is revoked on the
 *  next tick: the click has already consumed it by then, and holding it
 *  longer only leaks the blob. */
export function downloadText(filename: string, mime: string, content: string): void {
  const blob = new Blob([content], { type: mime })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.rel = 'noopener'
  document.body.append(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 0)
}

/** The filename a server chose, out of its ``Content-Disposition``.
 *  Returns '' when the header is absent or unparsable, so the caller
 *  can fall back to a name of its own rather than downloading
 *  something called "download". */
export function filenameFromContentDisposition(header: string | null): string {
  if (!header) return ''
  const m = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(header)
  return m ? decodeURIComponent(m[1]) : ''
}

/** Save an already-fetched response body under the name the server
 *  asked for, falling back to ``fallback``. Separate from
 *  {@link downloadText} because the bytes came off the network: the
 *  caller has already checked ``res.ok`` and handled the error body. */
export async function downloadResponse(res: Response, fallback: string): Promise<void> {
  const blob = await res.blob()
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filenameFromContentDisposition(res.headers.get('content-disposition')) || fallback
  a.rel = 'noopener'
  document.body.append(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 0)
}
