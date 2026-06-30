import { useCallback, useEffect, useState } from 'react'
import { api, errMessage, workspaceHeader } from '../api/client'
import { useSession } from '../auth/useSession'

// Per-workspace semantic-similarity floor for memory retrieval (cosine,
// 0..1). 0 disables the gate (every vector neighbour is kept, the
// historical behaviour); a positive value drops far semantic neighbours
// so a keyword / proper-noun query is not flooded by noise that ties
// with the real lexical hits under rank-only RRF. Lexical (keyword)
// matches are NEVER gated, so keyword search stays complete. Admin knob,
// tuned live; optimistic-concurrency guarded like the other settings.
export function RetrievalSettings() {
  const session = useSession()
  const activeId = session?.workspaceId
  const [floor, setFloor] = useState('0')
  // estimate_presets is a required field of the settings PATCH; restate
  // the current value so saving the floor doesn't clobber it (same
  // pattern as DispatchPanel).
  const [presets, setPresets] = useState<string[]>([])
  const [version, setVersion] = useState<number | null>(null)
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
    setFloor(String(data.settings?.retrieval_semantic_min_similarity ?? 0))
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
        setFloor(String(data.settings?.retrieval_semantic_min_similarity ?? 0))
        setPresets(data.settings?.estimate_presets ?? [])
        setVersion(data.version)
      }
    })()
    return () => {
      active = false
    }
  }, [activeId])

  async function save() {
    if (version === null) return
    const v = Number(floor.replace(',', '.'))
    if (!Number.isFinite(v) || v < 0 || v > 1) {
      setErr('Threshold must be between 0 and 1')
      return
    }
    setErr(null)
    setMsg(null)
    const { error, response } = await api.PATCH('/workspaces/me/settings', {
      params: { header: workspaceHeader() },
      body: {
        expected_version: version,
        estimate_presets: presets,
        retrieval_semantic_min_similarity: v,
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
      <h2>Memory retrieval</h2>
      <p className="hint">
        Semantic similarity floor (cosine, 0–1). 0 = off (every vector
        neighbour kept). Raise it if memory search returns results that
        don&apos;t match the query (far semantic neighbours); lower it if
        relevant semantic matches start disappearing. Keyword matches are
        never filtered.
      </p>
      {err && <p className="err">{err}</p>}
      {msg && <p className="ok">{msg}</p>}
      <div className="row">
        <input
          type="number"
          min={0}
          max={1}
          step="0.05"
          value={floor}
          onChange={(e) => setFloor(e.target.value)}
          aria-label="Semantic similarity floor"
        />
        <button type="button" className="btn--sm" onClick={() => void save()}>
          Save
        </button>
      </div>
    </section>
  )
}
