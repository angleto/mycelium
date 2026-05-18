import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import { useSession } from '../auth/useSession'
import type { components } from '../api/schema'

type Workflow = components['schemas']['WorkflowOut']
type State = components['schemas']['StateOut']
type Edge = components['schemas']['TransitionOut']
type WfMeta = { states: State[]; edges: Edge[] }
type StateRow = { name: string; is_initial: boolean; is_terminal: boolean }
type Transition = { from_state: string; to_state: string }

// WorkflowDefinition editor. The backend enforces "exactly one initial
// state" (workflow.invalid) and the transition rules; errors surface
// here verbatim from the i18n catalog.
export function WorkflowsRoute() {
  const { t } = useTranslation()
  const session = useSession()
  const activeId = session?.workspaceId
  const [list, setList] = useState<Workflow[]>([])
  const [meta, setMeta] = useState<Record<string, WfMeta>>({})
  const [name, setName] = useState('')
  // New-workflow template mirrors the system default
  // (todo -> in_progress -> done) so it is not misleading.
  const [states, setStates] = useState<StateRow[]>([
    { name: 'todo', is_initial: true, is_terminal: false },
    { name: 'in_progress', is_initial: false, is_terminal: false },
    { name: 'done', is_initial: false, is_terminal: true },
  ])
  const [transitions, setTransitions] = useState<Transition[]>([
    { from_state: 'todo', to_state: 'in_progress' },
    { from_state: 'in_progress', to_state: 'done' },
    { from_state: 'in_progress', to_state: 'todo' },
    { from_state: 'done', to_state: 'in_progress' },
  ])
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const loadMeta = useCallback(async (wfs: Workflow[]) => {
    const h = workspaceHeader()
    const entries = await Promise.all(
      wfs.map(async (w) => {
        const [st, tr] = await Promise.all([
          api.GET('/workflows/{workflow_id}/states', {
            params: { header: h, path: { workflow_id: w.id } },
          }),
          api.GET('/workflows/{workflow_id}/transitions', {
            params: { header: h, path: { workflow_id: w.id } },
          }),
        ])
        return [w.id, { states: st.data ?? [], edges: tr.data ?? [] }] as const
      }),
    )
    setMeta(Object.fromEntries(entries))
  }, [])

  const load = useCallback(async () => {
    const { data, error } = await api.GET('/workflows', {
      params: { header: workspaceHeader() },
    })
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    setList(data)
    await loadMeta(data)
  }, [loadMeta])

  useEffect(() => {
    let active = true
    void (async () => {
      const { data } = await api.GET('/workflows', {
        params: { header: workspaceHeader() },
      })
      if (!active || !data) return
      setList(data)
      await loadMeta(data)
    })()
    return () => {
      active = false
    }
  }, [activeId, loadMeta])

  function moveState(i: number, dir: -1 | 1) {
    setStates((rs) => {
      const j = i + dir
      if (j < 0 || j >= rs.length) return rs
      const next = [...rs]
      const tmp = next[i]
      next[i] = next[j]
      next[j] = tmp
      return next
    })
  }

  function setState(i: number, patch: Partial<StateRow>) {
    setStates((rs) => rs.map((r, j) => (j === i ? { ...r, ...patch } : r)))
  }

  async function onCreate(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setErr(null)
    const { error } = await api.POST('/workflows', {
      params: { header: workspaceHeader() },
      body: {
        name,
        states: states.map((s, i) => ({
          name: s.name,
          ord: i,
          is_initial: s.is_initial,
          is_terminal: s.is_terminal,
        })),
        transitions,
      },
    })
    setBusy(false)
    if (error) {
      setErr(errMessage(error))
      return
    }
    setName('')
    await load()
  }

  return (
    <section className="card">
      <h1>{t('workflows.title')}</h1>
      {list.length === 0 ? (
        <p className="hint">{t('workflows.none')}</p>
      ) : (
        <ul className="list">
          {list.map((w) => {
            const m = meta[w.id]
            const nameOf = (id: string) =>
              m?.states.find((s) => s.id === id)?.name ?? id.slice(0, 6)
            return (
              <li key={w.id}>
                <strong>{w.name}</strong>
                {w.is_default && (
                  <span className="muted"> · {t('workflows.isDefault')}</span>
                )}
                {m && (
                  <div className="row" style={{ flexWrap: 'wrap' }}>
                    {m.states.map((s) => (
                      <span key={s.id} className="chip">
                        {s.name}
                        {s.is_initial ? ` · ${t('workflows.initial')}` : ''}
                        {s.is_terminal ? ` · ${t('workflows.terminal')}` : ''}
                      </span>
                    ))}
                  </div>
                )}
                {m && m.edges.length > 0 && (
                  <div className="muted" style={{ fontSize: '0.8rem' }}>
                    {m.edges
                      .map(
                        (e) =>
                          `${nameOf(e.from_state_id)} → ${nameOf(e.to_state_id)}`,
                      )
                      .join(' , ')}
                  </div>
                )}
              </li>
            )
          })}
        </ul>
      )}

      <form onSubmit={(e) => void onCreate(e)}>
        <label>
          {t('workflows.name')}
          <input required value={name} onChange={(e) => setName(e.target.value)} />
        </label>

        <h2>{t('workflows.states')}</h2>
        {states.map((s, i) => (
          <div className="row" key={i}>
            <input
              required
              placeholder={t('workflows.stateName')}
              value={s.name}
              onChange={(e) => setState(i, { name: e.target.value })}
            />
            <label>
              <input
                type="checkbox"
                checked={s.is_initial}
                onChange={(e) => setState(i, { is_initial: e.target.checked })}
              />{' '}
              {t('workflows.initial')}
            </label>
            <label>
              <input
                type="checkbox"
                checked={s.is_terminal}
                onChange={(e) => setState(i, { is_terminal: e.target.checked })}
              />{' '}
              {t('workflows.terminal')}
            </label>
            <button
              type="button"
              className="btn--ghost btn--sm"
              disabled={i === 0}
              onClick={() => moveState(i, -1)}
              aria-label="move up"
            >
              ↑
            </button>
            <button
              type="button"
              className="btn--ghost btn--sm"
              disabled={i === states.length - 1}
              onClick={() => moveState(i, 1)}
              aria-label="move down"
            >
              ↓
            </button>
          </div>
        ))}
        <button
          type="button"
          onClick={() =>
            setStates((rs) => [...rs, { name: '', is_initial: false, is_terminal: false }])
          }
        >
          {t('workflows.addState')}
        </button>

        <h2>{t('workflows.transitions')}</h2>
        {transitions.map((tr, i) => (
          <div className="row" key={i}>
            <input
              placeholder={t('workflows.from')}
              value={tr.from_state}
              onChange={(e) =>
                setTransitions((xs) =>
                  xs.map((x, j) => (j === i ? { ...x, from_state: e.target.value } : x)),
                )
              }
            />
            <input
              placeholder={t('workflows.to')}
              value={tr.to_state}
              onChange={(e) =>
                setTransitions((xs) =>
                  xs.map((x, j) => (j === i ? { ...x, to_state: e.target.value } : x)),
                )
              }
            />
          </div>
        ))}
        <button
          type="button"
          onClick={() =>
            setTransitions((xs) => [...xs, { from_state: '', to_state: '' }])
          }
        >
          {t('workflows.addTransition')}
        </button>

        {err && <p className="err">{err}</p>}
        <button type="submit" disabled={busy}>
          {busy ? t('workflows.creating') : t('workflows.create')}
        </button>
      </form>
    </section>
  )
}
