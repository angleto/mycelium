import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { saveWorkspaceSettings, useMyWorkspace } from '../auth/useMyWorkspace'
import { formatHours } from '../lib/estimate'

// Per-workspace task-estimate presets (the task form dropdown values).
// Shared workspace settings, written through the shared snapshot so the
// version this card sends is always the one the last write returned.
export function EstimatePresets() {
  const { t } = useTranslation()
  const { ws } = useMyWorkspace()
  const [add, setAdd] = useState('')
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)

  // Decimal serializes as string over the API; keep presets as strings
  // end-to-end (like the task estimate field).
  const presets = (ws?.settings?.estimate_presets ?? []).map(String)

  async function save(next: string[]) {
    setBusy(true)
    setErr(null)
    setMsg(null)
    const res = await saveWorkspaceSettings({ estimate_presets: next })
    setBusy(false)
    if (!res.ok) {
      setErr(res.message)
      return
    }
    setMsg(t('tasks.saved'))
  }

  function onAdd() {
    const v = Number(add.replace(',', '.'))
    if (!Number.isFinite(v) || v <= 0) return
    setAdd('')
    void save([...presets, String(v)])
  }

  return (
    <section className="card">
      <h2>{t('estpre.title')}</h2>
      <p className="hint">{t('estpre.note')}</p>
      {err && <p className="err">{err}</p>}
      {msg && <p className="ok">{msg}</p>}
      <ul className="list">
        {presets.map((p, i) => (
          <li key={`${p}-${i}`}>
            <span className="chip">{formatHours(Number(p))}</span>
            <button
              type="button"
              className="btn--ghost btn--sm"
              disabled={busy}
              onClick={() => void save(presets.filter((_, j) => j !== i))}
            >
              {t('estpre.remove')}
            </button>
          </li>
        ))}
      </ul>
      <div className="row">
        <input
          type="number"
          min={0}
          step="0.25"
          placeholder={t('estpre.addHours')}
          value={add}
          onChange={(e) => setAdd(e.target.value)}
        />
        <button type="button" className="btn--sm" disabled={busy} onClick={onAdd}>
          {t('estpre.add')}
        </button>
      </div>
    </section>
  )
}
