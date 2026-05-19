import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { authFetch, errMessage } from '../api/client'

// Attachments on a note OR a task (exactly one parent). Binary
// upload/download go through authFetch (raw, authenticated) since the
// typed JSON client does not fit multipart/blob. Images get an inline
// thumbnail + click-to-zoom lightbox; other files are a chip with a
// download action. Mirrors the costa_associati UX.

type AttachmentMeta = {
  id: string
  filename: string
  mime_type: string
  size_bytes: number
  created_at: string
}

function humanSize(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`
  return `${(n / (1024 * 1024)).toFixed(1)} MB`
}

const isImage = (m: string) => m.startsWith('image/')

export function Attachments({
  noteId,
  taskId,
}: {
  noteId?: string
  taskId?: string
}) {
  const { t } = useTranslation()
  const base = noteId
    ? `/notes/${noteId}/attachments`
    : `/tasks/${taskId}/attachments`

  const [items, setItems] = useState<AttachmentMeta[]>([])
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [thumbs, setThumbs] = useState<Record<string, string>>({})
  const [zoom, setZoom] = useState<{ url: string; name: string } | null>(null)
  const [tick, setTick] = useState(0)
  const fileInput = useRef<HTMLInputElement>(null)

  // List + image thumbnails. Re-runs on parent change or after a
  // mutation (tick). Object URLs are revoked on cleanup so a long
  // editing session does not leak blobs.
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
      for (const r of rows.filter((x) => isImage(x.mime_type))) {
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
                  onClick={() =>
                    setZoom({ url: thumbs[it.id], name: it.filename })
                  }
                >
                  <img src={thumbs[it.id]} alt={it.filename} />
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
      {zoom && (
        <div
          className="lightbox"
          role="dialog"
          aria-label={zoom.name}
          onClick={() => setZoom(null)}
        >
          <img src={zoom.url} alt={zoom.name} />
        </div>
      )}
    </div>
  )
}
