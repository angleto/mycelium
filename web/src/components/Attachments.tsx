import { useCallback, useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { authFetch, errMessage } from '../api/client'
import { useMediaQuery, MOBILE_QUERY } from '../lib/useMediaQuery'

// Attachments on a note OR a task (exactly one parent). Binary
// upload/download go through authFetch (raw, authenticated) since the
// typed JSON client does not fit multipart/blob.
//
// Preview affordances (ported from bitvision_phoenix's DocumentPreview):
//   - Images: inline 44x44 thumbnail (fetched eagerly), click opens a
//     lightbox with the full-size blob — same as before.
//   - PDFs: clickable "PDF" tile; on click we fetch the binary lazily,
//     wrap it in an iframe on desktop, or surface an "Open in new tab"
//     CTA on mobile (Safari/Chrome on phones do not render PDFs in
//     iframes reliably).
//   - Anything else: 📎 icon + download-only, as today.
//
// Object URLs never escape this component: the eager thumbs are
// revoked when the list refetches, and the modal-owned blob (PDF) is
// revoked when the modal closes. The bearer token is on authFetch's
// Authorization header — never on a DOM `src` attribute.

type AttachmentMeta = {
  id: string
  filename: string
  mime_type: string
  size_bytes: number
  created_at: string
}

// Hard cap mirroring bitvision's "binary" branch. Pulling >50 MiB
// through the browser to render a preview is wasteful even when the
// blob would render fine; force the user through Download instead.
const MAX_PREVIEW_BYTES = 50 * 1024 * 1024

function humanSize(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`
  return `${(n / (1024 * 1024)).toFixed(1)} MB`
}

const isImage = (m: string) => m.startsWith('image/')
const isPdf = (m: string, fn: string) =>
  m === 'application/pdf' || fn.toLowerCase().endsWith('.pdf')

type Preview =
  | { kind: 'image'; url: string; name: string; owned: false }
  | { kind: 'pdf'; url: string; name: string; owned: true }
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
        (x) => isImage(x.mime_type) && x.size_bytes <= MAX_PREVIEW_BYTES,
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

  function openImagePreview(it: AttachmentMeta) {
    const url = thumbs[it.id]
    if (!url) return
    setPreview({ kind: 'image', url, name: it.filename, owned: false })
  }

  async function openPdfPreview(it: AttachmentMeta) {
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
    setPreview({ kind: 'pdf', url, name: it.filename, owned: true })
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
          {items.map((it) => (
            <li key={it.id} className="att">
              {isImage(it.mime_type) && thumbs[it.id] ? (
                <button
                  type="button"
                  className="att__thumb"
                  title={t('attach.zoom')}
                  onClick={() => openImagePreview(it)}
                >
                  <img src={thumbs[it.id]} alt={it.filename} />
                </button>
              ) : isPdf(it.mime_type, it.filename) ? (
                <button
                  type="button"
                  className="att__thumb att__thumb--pdf"
                  title={t('attach.preview')}
                  aria-label={t('attach.preview')}
                  onClick={() => void openPdfPreview(it)}
                >
                  PDF
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
          ))}
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
