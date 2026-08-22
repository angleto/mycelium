import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { saveWorkspaceSettings, useMyWorkspace } from '../auth/useMyWorkspace'

// Per-workspace semantic-similarity floor for memory retrieval (cosine,
// 0..1). 0 disables the gate (every vector neighbour is kept, the
// historical behaviour); a positive value drops far semantic neighbours
// so a keyword / proper-noun query is not flooded by noise that ties
// with the real lexical hits under rank-only RRF. Lexical (keyword)
// matches are NEVER gated, so keyword search stays complete.
//
// Reads and writes the SHARED workspace snapshot: it used to fetch
// /workspaces/me for itself and hold its own `expected_version`, which
// meant saving in a sibling settings card left this one stale and its
// next save 409'd.
export function RetrievalSettings() {
  const { t } = useTranslation()
  const { ws } = useMyWorkspace()
  const [draft, setDraft] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)

  const stored = String(ws?.settings?.retrieval_semantic_min_similarity ?? 0)
  // Derived, not mirrored into state by an effect: the field shows the
  // user's edit while there is one, and the stored value otherwise — so
  // a save elsewhere is reflected without a second source of truth.
  const floor = draft ?? stored

  async function save() {
    const v = Number(floor.replace(',', '.'))
    if (!Number.isFinite(v) || v < 0 || v > 1) {
      setErr(t('retrieval.range'))
      return
    }
    setBusy(true)
    setErr(null)
    setMsg(null)
    const res = await saveWorkspaceSettings({
      retrieval_semantic_min_similarity: v,
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
      <h2>{t('retrieval.title')}</h2>
      <p className="hint">{t('retrieval.note')}</p>
      {err && <p className="err">{err}</p>}
      {msg && <p className="ok">{msg}</p>}
      <div className="row">
        <input
          type="number"
          min={0}
          max={1}
          step="0.05"
          value={floor}
          onChange={(e) => setDraft(e.target.value)}
          aria-label={t('retrieval.floorLabel')}
        />
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
