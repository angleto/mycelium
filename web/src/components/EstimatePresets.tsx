import { useCallback, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import { useSession } from '../auth/useSession'
import { formatHours } from '../lib/estimate'

// Per-workspace task-estimate presets (the task form dropdown values).
// Persisted and shared (workspace settings), optimistic-concurrency
// guarded like the workspace rename.
export function EstimatePresets() {
  const { t } = useTranslation()
  const session = useSession()
  const activeId = session?.workspaceId
  // Decimal serializes as string over the API; keep presets as
  // strings end-to-end (like the task estimate field).
  const [presets, setPresets] = useState<string[]>([])
  const [version, setVersion] = useState<number | null>(null)
  const [add, setAdd] = useState('')
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)

  const load = useCallback(async () => {
    const { data, error } = await api.GET('/workspaces/me', {
      params: { header: workspaceHeader() },
    })
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    setPresets(data.settings?.estimate_presets ?? [])
    setVersion(data.version)
  }, [])

  useEffect(() => {
    let active = true
    void (async () => {
      const { data } = await api.GET('/workspaces/me', {
        params: { header: workspaceHeader() },
      })
      if (active && data) {
        setPresets(data.settings?.estimate_presets ?? [])
        setVersion(data.version)
      }
    })()
    return () => {
      active = false
    }
  }, [activeId])

  async function save(next: string[]) {
    if (version === null) return
    setErr(null)
    setMsg(null)
    const { error, response } = await api.PATCH('/workspaces/me/settings', {
      params: { header: workspaceHeader() },
      body: { expected_version: version, estimate_presets: next },
    })
    if (response.status === 409) {
      setErr(t('tagmgr.conflict'))
      await load()
      return
    }
    if (error) {
      setErr(errMessage(error))
      return
    }
    setMsg(t('tasks.saved'))
    await load()
  }

  function onAdd() {
    const v = Number(add.replace(',', '.'))
    if (!Number.isFinite(v) || v <= 0) return
    setAdd('')
    void save([...presets, String(v)])
  }

  return (
    <section className="card">
      <h1>{t('estpre.title')}</h1>
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
        <button type="button" className="btn--sm" onClick={onAdd}>
          {t('estpre.add')}
        </button>
      </div>
    </section>
  )
}
