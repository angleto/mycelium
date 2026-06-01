import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { authFetch, errMessage } from '../api/client'
import {
  isImageMime,
  uploadAttachment,
  type ImageUploadParent,
  type UploadedAttachment,
} from '../lib/imageUpload'
import { invalidateAttachmentManifest } from '../lib/attachmentManifest'

// Picker invoked from the editor toolbar to link an attachment in the
// body. It lists the parent note/task's existing attachments and can
// upload a new one — either way the result is the SAME bearer-auth
// attachment, returned to the caller which inserts the markdown
// reference (image embed for image mimes, a download link otherwise).
// Nothing here exposes a public URL.

type Row = {
  id: string
  filename: string
  mime_type: string
  size_bytes: number
}

function humanSize(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`
  return `${(n / (1024 * 1024)).toFixed(1)} MB`
}

export function AttachmentPicker({
  parent,
  onPick,
  onClose,
}: {
  parent: ImageUploadParent
  onPick: (att: UploadedAttachment) => void
  onClose: () => void
}) {
  const { t } = useTranslation()
  const base =
    parent.kind === 'note'
      ? `/notes/${parent.id}/attachments`
      : `/tasks/${parent.id}/attachments`

  const [rows, setRows] = useState<Row[] | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const fileInput = useRef<HTMLInputElement>(null)

  useEffect(() => {
    let active = true
    void (async () => {
      const res = await authFetch(base)
      if (!active) return
      if (!res.ok) {
        setErr(errMessage(await res.json().catch(() => null)))
        setRows([])
        return
      }
      setRows((await res.json()) as Row[])
    })()
    return () => {
      active = false
    }
  }, [base])

  // Escape closes the picker.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  function pickRow(r: Row) {
    onPick({
      id: r.id,
      filename: r.filename,
      mimeType: r.mime_type,
      url: `/attachments/${r.id}/download`,
    })
    onClose()
  }

  async function onUpload(file: File) {
    setBusy(true)
    setErr(null)
    try {
      const up = await uploadAttachment(parent, file)
      invalidateAttachmentManifest(parent)
      onPick(up)
      onClose()
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div
      className="attref__backdrop"
      onClick={onClose}
      role="presentation"
    >
      <div
        className="attref"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label={t('attref.title')}
      >
        <div className="attref__head">
          <span className="attref__title">{t('attref.title')}</span>
          <button
            type="button"
            className="btn--ghost btn--sm"
            onClick={onClose}
            aria-label={t('attref.close')}
          >
            ✕
          </button>
        </div>

        {err && <p className="err">{err}</p>}

        {rows === null ? (
          <p className="hint">{t('attref.loading')}</p>
        ) : rows.length === 0 ? (
          <p className="hint">{t('attref.none')}</p>
        ) : (
          <ul className="attref__list">
            {rows.map((r) => (
              <li key={r.id}>
                <button
                  type="button"
                  className="attref__item"
                  onClick={() => pickRow(r)}
                  title={t('attref.insert')}
                >
                  <span className="attref__icon" aria-hidden>
                    {isImageMime(r.mime_type) ? '🖼' : '📎'}
                  </span>
                  <span className="attref__name">{r.filename}</span>
                  <span className="attref__size">{humanSize(r.size_bytes)}</span>
                </button>
              </li>
            ))}
          </ul>
        )}

        <div className="attref__foot">
          <button
            type="button"
            className="btn--sm"
            disabled={busy}
            onClick={() => fileInput.current?.click()}
          >
            {busy ? t('attref.uploading') : t('attref.uploadNew')}
          </button>
          <input
            ref={fileInput}
            type="file"
            hidden
            onChange={(e) => {
              const f = e.target.files?.[0]
              e.target.value = ''
              if (f) void onUpload(f)
            }}
          />
        </div>
      </div>
    </div>
  )
}
