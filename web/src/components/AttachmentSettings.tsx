import { useCallback, useEffect, useState } from 'react'
import { api, errMessage, workspaceHeader } from '../api/client'
import { useSession } from '../auth/useSession'

// Per-workspace cap on a single note/task attachment (the buffered
// upload path used by the file picker). The backend stores BYTES; this
// admin knob edits MiB for readability. The server bounds the value to a
// hard ceiling (the buffered path holds the whole file in memory), and
// reports both the effective cap and that ceiling on GET /workspaces/me.
// Optimistic-concurrency guarded like the other settings; estimate_presets
// is restated on save so it is not clobbered (same pattern as RetrievalSettings).
const MIB = 1024 * 1024

export function AttachmentSettings() {
  const session = useSession()
  const activeId = session?.workspaceId
  const [sizeMib, setSizeMib] = useState('10')
  const [ceilingMib, setCeilingMib] = useState(100)
  const [presets, setPresets] = useState<string[]>([])
  const [version, setVersion] = useState<number | null>(null)
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)

  const apply = useCallback(
    (data: {
      version: number
      settings?: {
        attachment_max_bytes?: number
        attachment_max_bytes_ceiling?: number
        estimate_presets?: string[]
      }
    }) => {
      setSizeMib(String(Math.round((data.settings?.attachment_max_bytes ?? 0) / MIB)))
      const ceil = data.settings?.attachment_max_bytes_ceiling ?? 0
      if (ceil > 0) setCeilingMib(Math.floor(ceil / MIB))
      setPresets(data.settings?.estimate_presets ?? [])
      setVersion(data.version)
    },
    [],
  )

  const load = useCallback(async () => {
    const { data, error } = await api.GET('/workspaces/me', {
      params: { header: workspaceHeader() },
    })
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    apply(data)
  }, [apply])

  useEffect(() => {
    let active = true
    void (async () => {
      const { data } = await api.GET('/workspaces/me', {
        params: { header: workspaceHeader() },
      })
      if (active && data) apply(data)
    })()
    return () => {
      active = false
    }
  }, [activeId, apply])

  async function save() {
    if (version === null) return
    const mib = Number(sizeMib.replace(',', '.'))
    if (!Number.isFinite(mib) || mib < 1 || mib > ceilingMib) {
      setErr(`Size must be between 1 and ${ceilingMib} MiB`)
      return
    }
    setErr(null)
    setMsg(null)
    const { error, response } = await api.PATCH('/workspaces/me/settings', {
      params: { header: workspaceHeader() },
      body: {
        expected_version: version,
        estimate_presets: presets,
        attachment_max_bytes: Math.round(mib) * MIB,
      },
    })
    if (response.status === 409) {
      setErr('Saved elsewhere — reloaded, retry')
      await load()
      return
    }
    if (error) {
      setErr(errMessage(error))
      return
    }
    setMsg('Saved')
    await load()
  }

  return (
    <section className="card">
      <h2>Attachments</h2>
      <p className="hint">
        Maximum size of a single attachment uploaded to a note or task, in MiB
        (1–{ceilingMib}). Raise it if uploads fail with &ldquo;exceeds the
        maximum size&rdquo;. The whole file is held in memory while it is
        stored, so the ceiling is set by the deployment.
      </p>
      {err && <p className="err">{err}</p>}
      {msg && <p className="ok">{msg}</p>}
      <div className="row">
        <input
          type="number"
          min={1}
          max={ceilingMib}
          step="1"
          value={sizeMib}
          onChange={(e) => setSizeMib(e.target.value)}
          aria-label="Maximum attachment size (MiB)"
        />
        <span className="hint">MiB</span>
        <button type="button" className="btn--sm" onClick={() => void save()}>
          Save
        </button>
      </div>
    </section>
  )
}
