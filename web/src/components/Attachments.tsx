import { useCallback, useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { authFetch, errMessage } from '../api/client'
import { useMediaQuery, MOBILE_QUERY } from '../lib/useMediaQuery'
import { attachmentKind } from '../lib/attachmentKind'
import { attachmentMarkdownRef } from '../lib/attachmentRef'

// Attachments on a note OR a task (exactly one parent). Binary
// upload/download go through authFetch (raw, authenticated) since the
// typed JSON client does not fit multipart/blob.
//
// Preview affordances dispatch on attachmentKind() — the same classifier
// the markdown `![]()` embed uses, so the panel and the body never
// disagree on a file's kind:
//   - Images: inline 44x44 thumbnail (fetched eagerly), click → lightbox
//     with the full-size blob.
//   - PDFs: clickable tile → iframe on desktop, "Open in new tab" CTA on
//     mobile (Safari/Chrome on phones do not render PDFs in iframes).
//   - Audio / Video: tile → an <audio>/<video> player in the lightbox.
//   - Text (txt/md/csv/json/code/…): tile → the contents in a monospace
//     panel (truncated past TEXT_PREVIEW_MAX_CHARS).
//   - Anything else: 📎 icon + download-only.
//
// Every row also carries "Copy ref": it puts that attachment's canonical
// markdown reference (attachmentMarkdownRef — the shape the MCP
// `attach_file` tool and the CLI also hand out) on the clipboard. The
// editor's 📎 toolbar action already inserts such a reference through the
// AttachmentPicker, but only into the body it is editing; the clipboard
// copy is what lets the reference travel — into a different note or task,
// into a message to an agent, into anything outside this page — and it is
// reachable from the panel itself, with no editor open. Pasted back into a
// body it needs no special handling at all: the reference IS the markdown
// that gets stored, so it lands as the image or the link it denotes.
//
// Object URLs never escape this component: the eager thumbs are revoked
// when the list refetches, and a modal-owned blob (pdf/audio/video) is
// revoked when the modal closes. The bearer token is on authFetch's
// Authorization header — never on a DOM `src` attribute.

type AttachmentMeta = {
  id: string
  filename: string
  mime_type: string
  size_bytes: number
  created_at: string
}

// Hard cap on bytes pulled through the browser to render a preview;
// past it we force the user through Download instead.
const MAX_PREVIEW_BYTES = 50 * 1024 * 1024

// Characters of a text file shown inline before truncation. Mirrors the
// markdown text-embed cap.
const TEXT_PREVIEW_MAX_CHARS = 256 * 1024

// How long the copy-ref badge replaces the button label. A failure sticks
// around longer: it is information the user has to read and act on, not a
// mere acknowledgement.
const COPY_FLASH_MS = 2000
const COPY_FAIL_FLASH_MS = 5000

function humanSize(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`
  return `${(n / (1024 * 1024)).toFixed(1)} MB`
}

type Preview =
  | { kind: 'image'; url: string; name: string; owned: false }
  | { kind: 'pdf'; url: string; name: string; owned: true }
  | { kind: 'audio'; url: string; name: string; owned: true }
  | { kind: 'video'; url: string; name: string; owned: true }
  | { kind: 'text'; text: string; name: string; truncated: boolean }
  | { kind: 'loading'; name: string }
  | { kind: 'error'; name: string; message: string }
  | { kind: 'too-large'; name: string }

export function Attachments({
  noteId,
  taskId,
}: {
  noteId?: string
  taskId?: string
}) {
  const { t } = useTranslation()
  const isMobile = useMediaQuery(MOBILE_QUERY)
  const base = noteId
    ? `/notes/${noteId}/attachments`
    : `/tasks/${taskId}/attachments`

  const [items, setItems] = useState<AttachmentMeta[]>([])
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [thumbs, setThumbs] = useState<Record<string, string>>({})
  const [preview, setPreview] = useState<Preview | null>(null)
  const [tick, setTick] = useState(0)
  const fileInput = useRef<HTMLInputElement>(null)
  // Copy-ref flash for ONE row at a time (a click on another row replaces
  // it), with `ok` telling the truth about what happened: false means the
  // clipboard refused and the user had to copy the text by hand.
  const [copied, setCopied] = useState<{ id: string; ok: boolean } | null>(null)
  const copyTimer = useRef<number | undefined>(undefined)

  // List + image thumbnails. Re-runs on parent change or after a
  // mutation (tick). Object URLs are revoked on cleanup so a long
  // editing session does not leak blobs. Images over the preview cap
  // are skipped here — the user still sees the row + Download, just
  // not an eagerly-fetched thumbnail.
  useEffect(() => {
    let active = true
    const made: string[] = []
    void (async () => {
      const res = await authFetch(base)
      if (!active) return
      if (!res.ok) {
        setErr(errMessage(await res.json().catch(() => null)))
        return
      }
      const rows = (await res.json()) as AttachmentMeta[]
      if (!active) return
      setErr(null)
      setItems(rows)
      const next: Record<string, string> = {}
      for (const r of rows.filter(
        (x) =>
          attachmentKind(x.mime_type, x.filename) === 'image' &&
          x.size_bytes <= MAX_PREVIEW_BYTES,
      )) {
        const b = await authFetch(`/attachments/${r.id}/download`)
        if (!active) break
        if (b.ok) {
          const u = URL.createObjectURL(await b.blob())
          made.push(u)
          next[r.id] = u
        }
      }
      if (active) setThumbs(next)
    })()
    return () => {
      active = false
      for (const u of made) URL.revokeObjectURL(u)
    }
  }, [base, tick])

  // Close + revoke any preview-owned blob URL. Images reuse the
  // thumbs[id] URL that the list-effect already owns, so the modal
  // must never revoke them — only the `owned` branch revokes.
  const closePreview = useCallback(() => {
    setPreview((p) => {
      if (p && 'owned' in p && p.owned) URL.revokeObjectURL(p.url)
      return null
    })
  }, [])

  // Escape key closes the preview. Attached only while a preview is
  // open to avoid grabbing the key in other contexts.
  useEffect(() => {
    if (!preview) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') closePreview()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [preview, closePreview])

  // Drop a pending flash timer on unmount — the panel is remounted on
  // every note/task navigation, and a surviving timeout would setState on
  // a dead component. The ref OBJECT is captured (not `.current`) so the
  // cleanup reads the timer that is actually pending.
  useEffect(() => {
    const timer = copyTimer
    return () => window.clearTimeout(timer.current)
  }, [])

  function openImagePreview(it: AttachmentMeta) {
    const url = thumbs[it.id]
    if (!url) return
    setPreview({ kind: 'image', url, name: it.filename, owned: false })
  }

  // Fetch the binary lazily and show it in the lightbox. pdf/audio/video
  // each own their object URL (revoked on close).
  async function openBlobPreview(
    it: AttachmentMeta,
    kind: 'pdf' | 'audio' | 'video',
  ) {
    if (it.size_bytes > MAX_PREVIEW_BYTES) {
      setPreview({ kind: 'too-large', name: it.filename })
      return
    }
    setPreview({ kind: 'loading', name: it.filename })
    const res = await authFetch(`/attachments/${it.id}/download`)
    if (!res.ok) {
      setPreview({
        kind: 'error',
        name: it.filename,
        message: `HTTP ${res.status}`,
      })
      return
    }
    const url = URL.createObjectURL(await res.blob())
    setPreview({ kind, url, name: it.filename, owned: true })
  }

  // Text preview: fetch + decode, capped to TEXT_PREVIEW_MAX_CHARS. Held
  // as a string (not an object URL), so there is nothing to revoke.
  async function openTextPreview(it: AttachmentMeta) {
    if (it.size_bytes > MAX_PREVIEW_BYTES) {
      setPreview({ kind: 'too-large', name: it.filename })
      return
    }
    setPreview({ kind: 'loading', name: it.filename })
    const res = await authFetch(`/attachments/${it.id}/download`)
    if (!res.ok) {
      setPreview({
        kind: 'error',
        name: it.filename,
        message: `HTTP ${res.status}`,
      })
      return
    }
    const raw = await res.text()
    const truncated = raw.length > TEXT_PREVIEW_MAX_CHARS
    setPreview({
      kind: 'text',
      name: it.filename,
      text: truncated ? raw.slice(0, TEXT_PREVIEW_MAX_CHARS) : raw,
      truncated,
    })
  }

  async function onPick(f: File) {
    setBusy(true)
    setErr(null)
    const body = new FormData()
    body.append('file', f)
    const res = await authFetch(base, { method: 'POST', body })
    setBusy(false)
    if (!res.ok) {
      setErr(errMessage(await res.json().catch(() => null)))
      return
    }
    setTick((n) => n + 1)
  }

  async function onDelete(id: string) {
    if (!window.confirm(t('attach.confirmDelete'))) return
    const res = await authFetch(`/attachments/${id}`, { method: 'DELETE' })
    if (!res.ok && res.status !== 204) {
      setErr(errMessage(await res.json().catch(() => null)))
      return
    }
    setTick((n) => n + 1)
  }

  // Put `text` on the clipboard, degrading through every path a browser
  // may still leave open — the async Clipboard API is unavailable outside
  // a secure context (a plain-http deployment has no `navigator.clipboard`
  // at all) and rejects when the permission is denied or the document is
  // not focused:
  //   1. navigator.clipboard.writeText — the modern, permissioned path;
  //   2. a throwaway textarea + execCommand('copy') — deprecated, but the
  //      only thing that works on http. Still inside the click gesture, so
  //      the transient user activation it requires is alive;
  //   3. window.prompt with the text preselected — no automatic copy, yet
  //      the string is in front of the user, who can select and copy it.
  // Returns whether the text reached the clipboard WITHOUT manual work, so
  // the caller can be honest in the UI instead of flashing a lying
  // "Copied". The one thing that never happens is a silent no-op.
  async function copyToClipboard(text: string): Promise<boolean> {
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text)
        return true
      }
    } catch {
      /* insecure context / denied / unfocused — try the fallbacks */
    }
    // Built outside the try so the finally below can always take it back
    // out: if execCommand throws, a textarea left in the body would keep
    // the (focused, off-screen) selection and swallow the keyboard.
    const ta = document.createElement('textarea')
    try {
      ta.value = text
      // Off-screen but still rendered and focusable: `hidden` or
      // display:none makes the selection — and therefore the copy — a
      // no-op. readonly keeps the mobile keyboard from popping up.
      ta.setAttribute('readonly', '')
      ta.style.position = 'fixed'
      ta.style.top = '-1000px'
      ta.style.opacity = '0'
      document.body.appendChild(ta)
      ta.select()
      if (document.execCommand('copy')) return true
    } catch {
      /* execCommand unsupported or blocked — fall through to the prompt */
    } finally {
      ta.remove()
    }
    try {
      window.prompt(t('attach.copyRefManual'), text)
    } catch {
      /* modals blocked (sandboxed frame): the failed badge is all we have */
    }
    return false
  }

  // Copy this row's paste-ready markdown reference. The string comes from
  // attachmentRef.ts, the single place the web builds one (the editor's
  // attach picker inserts the very same reference through it).
  async function onCopyRef(it: AttachmentMeta) {
    const ok = await copyToClipboard(attachmentMarkdownRef(it))
    setCopied({ id: it.id, ok })
    window.clearTimeout(copyTimer.current)
    copyTimer.current = window.setTimeout(
      () => setCopied(null),
      ok ? COPY_FLASH_MS : COPY_FAIL_FLASH_MS,
    )
  }

  async function onDownload(it: AttachmentMeta) {
    const res = await authFetch(`/attachments/${it.id}/download`)
    if (!res.ok) return
    const u = URL.createObjectURL(await res.blob())
    const a = document.createElement('a')
    a.href = u
    a.download = it.filename
    a.click()
    // Defer revoke: revoking immediately can cancel the download.
    window.setTimeout(() => URL.revokeObjectURL(u), 60000)
  }

  return (
    <div className="atts">
      <div className="atts__head">
        <span className="atts__lbl">{t('attach.title')}</span>
        <button
          type="button"
          className="btn--sm"
          disabled={busy}
          onClick={() => fileInput.current?.click()}
        >
          {busy ? t('attach.uploading') : t('attach.add')}
        </button>
        <input
          ref={fileInput}
          type="file"
          hidden
          onChange={(e) => {
            const f = e.target.files?.[0]
            e.target.value = ''
            if (f) void onPick(f)
          }}
        />
      </div>
      {err && <p className="err">{err}</p>}
      {items.length === 0 ? (
        <p className="hint">{t('attach.none')}</p>
      ) : (
        <ul className="atts__list">
          {items.map((it) => {
            const kind = attachmentKind(it.mime_type, it.filename)
            // Copy-ref outcome: replaces the button's visible label (and
            // its tooltip) while it lasts. The button carries no
            // aria-label, exactly like its Download / Remove siblings, so
            // its accessible name IS the text on it — "Copy ref" idle,
            // "Copied" / "Copy failed" during the flash — and the two can
            // never diverge (WCAG 2.5.3 Label in Name). The long hint
            // stays in `title`, which a button with text content exposes
            // as a description, not as its name.
            const copyFlash =
              copied?.id === it.id
                ? copied.ok
                  ? t('attach.copyRefDone')
                  : t('attach.copyRefFailed')
                : null
            return (
            <li key={it.id} className="att">
              {kind === 'image' && thumbs[it.id] ? (
                <button
                  type="button"
                  className="att__thumb"
                  title={t('attach.zoom')}
                  onClick={() => openImagePreview(it)}
                >
                  <img src={thumbs[it.id]} alt={it.filename} />
                </button>
              ) : kind === 'pdf' ? (
                <button
                  type="button"
                  className="att__thumb att__thumb--pdf"
                  title={t('attach.preview')}
                  aria-label={t('attach.preview')}
                  onClick={() => void openBlobPreview(it, 'pdf')}
                >
                  PDF
                </button>
              ) : kind === 'audio' ? (
                <button
                  type="button"
                  className="att__thumb att__thumb--audio"
                  title={t('attach.preview')}
                  aria-label={t('attach.preview')}
                  onClick={() => void openBlobPreview(it, 'audio')}
                >
                  ♪
                </button>
              ) : kind === 'video' ? (
                <button
                  type="button"
                  className="att__thumb att__thumb--video"
                  title={t('attach.preview')}
                  aria-label={t('attach.preview')}
                  onClick={() => void openBlobPreview(it, 'video')}
                >
                  ▶
                </button>
              ) : kind === 'text' ? (
                <button
                  type="button"
                  className="att__thumb att__thumb--text"
                  title={t('attach.preview')}
                  aria-label={t('attach.preview')}
                  onClick={() => void openTextPreview(it)}
                >
                  TXT
                </button>
              ) : (
                <span className="att__icon" aria-hidden>
                  📎
                </span>
              )}
              <span className="att__name grow" title={it.filename}>
                {it.filename}
                <span className="muted"> · {humanSize(it.size_bytes)}</span>
              </span>
              <button
                type="button"
                className="btn--sm btn--ghost"
                title={copyFlash ?? t('attach.copyRefHint')}
                onClick={() => void onCopyRef(it)}
              >
                {copyFlash ?? t('attach.copyRef')}
              </button>
              <button
                type="button"
                className="btn--sm btn--ghost"
                onClick={() => void onDownload(it)}
              >
                {t('attach.download')}
              </button>
              <button
                type="button"
                className="btn--sm btn--danger"
                onClick={() => void onDelete(it.id)}
              >
                {t('attach.remove')}
              </button>
            </li>
            )
          })}
        </ul>
      )}
      {preview && (
        <div
          className="lightbox"
          role="dialog"
          aria-label={preview.name}
          onClick={closePreview}
        >
          {preview.kind === 'image' && (
            <img src={preview.url} alt={preview.name} />
          )}
          {preview.kind === 'pdf' && !isMobile && (
            // Clicks INSIDE the iframe do not bubble (browser
            // isolates the document); clicks on the iframe element
            // itself (border, scrollbar) would, so stop propagation
            // here to keep the backdrop-click-to-close UX without the
            // PDF chrome closing the modal accidentally.
            <iframe
              src={preview.url}
              title={preview.name}
              className="lightbox__pdf"
              onClick={(e) => e.stopPropagation()}
            />
          )}
          {preview.kind === 'pdf' && isMobile && (
            <div
              className="lightbox__panel"
              onClick={(e) => e.stopPropagation()}
            >
              <p>
                <strong>{t('attach.pdfMobileTitle')}</strong>
              </p>
              <p className="hint">{t('attach.pdfMobileHint')}</p>
              <a
                href={preview.url}
                target="_blank"
                rel="noreferrer noopener"
                className="btn--sm"
              >
                {t('attach.pdfOpenInNewTab')}
              </a>
              <button
                type="button"
                className="btn--sm btn--ghost"
                onClick={closePreview}
              >
                {t('attach.close')}
              </button>
            </div>
          )}
          {preview.kind === 'audio' && (
            <div
              className="lightbox__panel"
              onClick={(e) => e.stopPropagation()}
            >
              <p>
                <strong>{preview.name}</strong>
              </p>
              <audio src={preview.url} controls autoPlay />
              <button
                type="button"
                className="btn--sm btn--ghost"
                onClick={closePreview}
              >
                {t('attach.close')}
              </button>
            </div>
          )}
          {preview.kind === 'video' && (
            <div
              className="lightbox__panel lightbox__panel--video"
              onClick={(e) => e.stopPropagation()}
            >
              <video src={preview.url} controls autoPlay className="lightbox__video" />
            </div>
          )}
          {preview.kind === 'text' && (
            <div
              className="lightbox__panel lightbox__panel--text"
              onClick={(e) => e.stopPropagation()}
            >
              <p>
                <strong>{preview.name}</strong>
              </p>
              <pre className="lightbox__text">{preview.text}</pre>
              {preview.truncated && (
                <p className="hint">{t('attach.textTruncated')}</p>
              )}
              <button
                type="button"
                className="btn--sm"
                onClick={closePreview}
              >
                {t('attach.close')}
              </button>
            </div>
          )}
          {preview.kind === 'loading' && (
            <div
              className="lightbox__panel"
              onClick={(e) => e.stopPropagation()}
            >
              <p>{t('attach.previewLoading')}</p>
            </div>
          )}
          {preview.kind === 'error' && (
            <div
              className="lightbox__panel"
              onClick={(e) => e.stopPropagation()}
            >
              <p className="err">{t('attach.previewError')}</p>
              <p className="hint">{preview.message}</p>
              <button
                type="button"
                className="btn--sm"
                onClick={closePreview}
              >
                {t('attach.close')}
              </button>
            </div>
          )}
          {preview.kind === 'too-large' && (
            <div
              className="lightbox__panel"
              onClick={(e) => e.stopPropagation()}
            >
              <p>{t('attach.previewTooLarge')}</p>
              <button
                type="button"
                className="btn--sm"
                onClick={closePreview}
              >
                {t('attach.close')}
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
