import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { saveWorkspaceSettings, useMyWorkspace } from '../auth/useMyWorkspace'

// Per-workspace cap on a single note/task attachment (the buffered
// upload path used by the file picker). The backend stores BYTES; this
// knob edits MiB for readability. The server bounds the value to a hard
// ceiling (the buffered path holds the whole file in memory) and reports
// both the effective cap and that ceiling on GET /workspaces/me.
//
// Reads and writes the SHARED workspace snapshot (see RetrievalSettings
// for why a per-card copy of `expected_version` was wrong).
const MIB = 1024 * 1024

export function AttachmentSettings() {
  const { t } = useTranslation()
  const { ws } = useMyWorkspace()
  const [draft, setDraft] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)

  const ceilingBytes = ws?.settings?.attachment_max_bytes_ceiling ?? 0
  const ceilingMib = ceilingBytes > 0 ? Math.floor(ceilingBytes / MIB) : 100
  const storedMib = String(
    Math.round((ws?.settings?.attachment_max_bytes ?? 0) / MIB),
  )
  const sizeMib = draft ?? storedMib

  async function save() {
    const mib = Number(sizeMib.replace(',', '.'))
    if (!Number.isFinite(mib) || mib < 1 || mib > ceilingMib) {
      setErr(t('attachcfg.range', { max: ceilingMib }))
      return
    }
    setBusy(true)
    setErr(null)
    setMsg(null)
    const res = await saveWorkspaceSettings({
      attachment_max_bytes: Math.round(mib) * MIB,
    })
    setBusy(false)
    if (!res.ok) {
      setErr(res.message)
      return
    }
    setDraft(null)
    setMsg(t('retrieval.saved'))
  }

  return (
    <section className="card">
      <h2>{t('attachcfg.title')}</h2>
      <p className="hint">{t('attachcfg.note', { max: ceilingMib })}</p>
      {err && <p className="err">{err}</p>}
      {msg && <p className="ok">{msg}</p>}
      <div className="row">
        <input
          type="number"
          min={1}
          max={ceilingMib}
          step="1"
          value={sizeMib}
          onChange={(e) => setDraft(e.target.value)}
          aria-label={t('attachcfg.sizeLabel')}
        />
        <span className="hint">MiB</span>
        <button
          type="button"
          className="btn--sm"
          disabled={busy || !ws}
          onClick={() => void save()}
        >
          {busy ? t('wsmgr.saving') : t('wsmgr.save')}
        </button>
      </div>
    </section>
  )
}
