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
type EditRow = StateRow & { id?: string }
type Edit = { id: string; name: string; states: EditRow[]; tr: Transition[] }

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
  const [editing, setEditing] = useState<Edit | null>(null)
  const [showCreate, setShowCreate] = useState(false)

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
    setShowCreate(false)
    await load()
  }

  function openEdit(w: Workflow) {
    const m = meta[w.id]
    if (!m) return
    const nameById = new Map(m.states.map((s) => [s.id, s.name]))
    setEditing({
      id: w.id,
      name: w.name,
      states: [...m.states]
        .sort((a, b) => a.ord - b.ord)
        .map((s) => ({
          id: s.id,
          name: s.name,
          is_initial: s.is_initial,
          is_terminal: s.is_terminal,
        })),
      tr: m.edges.map((e) => ({
        from_state: nameById.get(e.from_state_id) ?? '',
        to_state: nameById.get(e.to_state_id) ?? '',
      })),
    })
    setErr(null)
  }

  function patchE(p: Partial<Edit>) {
    setEditing((e) => (e ? { ...e, ...p } : e))
  }

  async function saveEdit() {
    if (!editing) return
    setBusy(true)
    setErr(null)
    const { error } = await api.PATCH('/workflows/{workflow_id}', {
      params: { header: workspaceHeader(), path: { workflow_id: editing.id } },
      body: {
        name: editing.name,
        states: editing.states.map((s, i) => ({
          id: s.id,
          name: s.name,
          ord: i,
          is_initial: s.is_initial,
          is_terminal: s.is_terminal,
        })),
        transitions: editing.tr.filter((x) => x.from_state && x.to_state),
      },
    })
    setBusy(false)
    if (error) {
      setErr(errMessage(error))
      return
    }
    setEditing(null)
    await load()
  }

  async function setDefault(id: string) {
    setErr(null)
    const { error } = await api.POST('/workflows/{workflow_id}/default', {
      params: { header: workspaceHeader(), path: { workflow_id: id } },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    await load()
  }

  async function removeWf(w: Workflow) {
    if (!window.confirm(t('workflows.confirmDelete', { name: w.name }))) return
    setErr(null)
    const { error } = await api.DELETE('/workflows/{workflow_id}', {
      params: { header: workspaceHeader(), path: { workflow_id: w.id } },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
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
                <div className="row">
                  <button
                    type="button"
                    className="btn--ghost btn--sm"
                    onClick={() => openEdit(w)}
                  >
                    {t('workflows.edit')}
                  </button>
                  {!w.is_default && (
                    <button
                      type="button"
                      className="btn--ghost btn--sm"
                      onClick={() => void setDefault(w.id)}
                    >
                      {t('workflows.makeDefault')}
                    </button>
                  )}
                  {!w.is_default && (
                    <button
                      type="button"
                      className="btn--danger btn--sm"
                      onClick={() => void removeWf(w)}
                    >
                      {t('workflows.delete')}
                    </button>
                  )}
                </div>
                {editing?.id === w.id && (
                  <div className="card" style={{ marginTop: '0.5rem' }}>
                    <label>
                      {t('workflows.name')}
                      <input
                        value={editing.name}
                        onChange={(e) => patchE({ name: e.target.value })}
                      />
                    </label>
                    <h3>{t('workflows.states')}</h3>
                    {editing.states.map((s, i) => (
                      <div className="row" key={s.id ?? `n${i}`}>
                        <input
                          value={s.name}
                          onChange={(e) =>
                            patchE({
                              states: editing.states.map((x, j) =>
                                j === i ? { ...x, name: e.target.value } : x,
                              ),
                            })
                          }
                        />
                        <label>
                          <input
                            type="checkbox"
                            checked={s.is_initial}
                            onChange={(e) =>
                              patchE({
                                states: editing.states.map((x, j) =>
                                  j === i
                                    ? { ...x, is_initial: e.target.checked }
                                    : x,
                                ),
                              })
                            }
                          />{' '}
                          {t('workflows.initial')}
                        </label>
                        <label>
                          <input
                            type="checkbox"
                            checked={s.is_terminal}
                            onChange={(e) =>
                              patchE({
                                states: editing.states.map((x, j) =>
                                  j === i
                                    ? { ...x, is_terminal: e.target.checked }
                                    : x,
                                ),
                              })
                            }
                          />{' '}
                          {t('workflows.terminal')}
                        </label>
                        <button
                          type="button"
                          className="btn--ghost btn--sm"
                          disabled={i === 0}
                          onClick={() =>
                            patchE({
                              states: editing.states.map((x, j) =>
                                j === i - 1
                                  ? editing.states[i]
                                  : j === i
                                    ? editing.states[i - 1]
                                    : x,
                              ),
                            })
                          }
                        >
                          ↑
                        </button>
                        <button
                          type="button"
                          className="btn--ghost btn--sm"
                          disabled={i === editing.states.length - 1}
                          onClick={() =>
                            patchE({
                              states: editing.states.map((x, j) =>
                                j === i + 1
                                  ? editing.states[i]
                                  : j === i
                                    ? editing.states[i + 1]
                                    : x,
                              ),
                            })
                          }
                        >
                          ↓
                        </button>
                        <button
                          type="button"
                          className="btn--ghost btn--sm"
                          onClick={() =>
                            patchE({
                              states: editing.states.filter(
                                (_, j) => j !== i,
                              ),
                            })
                          }
                        >
                          ✕
                        </button>
                      </div>
                    ))}
                    <button
                      type="button"
                      className="btn--sm"
                      onClick={() =>
                        patchE({
                          states: [
                            ...editing.states,
                            {
                              name: '',
                              is_initial: false,
                              is_terminal: false,
                            },
                          ],
                        })
                      }
                    >
                      {t('workflows.addState')}
                    </button>
                    <h3>{t('workflows.transitions')}</h3>
                    {editing.tr.map((x, i) => {
                      const opts = editing.states
                        .map((s) => s.name.trim())
                        .filter((nm) => nm !== '')
                      return (
                      <div className="row" key={i}>
                        <select
                          value={x.from_state}
                          onChange={(e) =>
                            patchE({
                              tr: editing.tr.map((y, j) =>
                                j === i
                                  ? { ...y, from_state: e.target.value }
                                  : y,
                              ),
                            })
                          }
                        >
                          <option value="">{t('workflows.from')}</option>
                          {opts.map((nm) => (
                            <option key={nm} value={nm}>
                              {nm}
                            </option>
                          ))}
                        </select>
                        <span>→</span>
                        <select
                          value={x.to_state}
                          onChange={(e) =>
                            patchE({
                              tr: editing.tr.map((y, j) =>
                                j === i
                                  ? { ...y, to_state: e.target.value }
                                  : y,
                              ),
                            })
                          }
                        >
                          <option value="">{t('workflows.to')}</option>
                          {opts.map((nm) => (
                            <option key={nm} value={nm}>
                              {nm}
                            </option>
                          ))}
                        </select>
                        <button
                          type="button"
                          className="btn--ghost btn--sm"
                          onClick={() =>
                            patchE({
                              tr: editing.tr.filter((_, j) => j !== i),
                            })
                          }
                        >
                          ✕
                        </button>
                      </div>
                      )
                    })}
                    <button
                      type="button"
                      className="btn--sm"
                      onClick={() =>
                        patchE({
                          tr: [
                            ...editing.tr,
                            { from_state: '', to_state: '' },
                          ],
                        })
                      }
                    >
                      {t('workflows.addTransition')}
                    </button>
                    <div className="row">
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => void saveEdit()}
                      >
                        {busy ? t('workflows.creating') : t('workflows.save')}
                      </button>
                      <button
                        type="button"
                        className="btn--ghost"
                        onClick={() => setEditing(null)}
                      >
                        {t('workflows.cancel')}
                      </button>
                    </div>
                  </div>
                )}
              </li>
            )
          })}
        </ul>
      )}
      {err && <p className="err">{err}</p>}

      {!showCreate && (
        <button type="button" onClick={() => setShowCreate(true)}>
          {t('workflows.newWorkflow')}
        </button>
      )}

      {showCreate && (
      <form onSubmit={(e) => void onCreate(e)}>
        <h2>{t('workflows.newWorkflow')}</h2>
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
        {transitions.map((tr, i) => {
          const opts = states
            .map((s) => s.name.trim())
            .filter((nm) => nm !== '')
          return (
            <div className="row" key={i}>
              <select
                value={tr.from_state}
                onChange={(e) =>
                  setTransitions((xs) =>
                    xs.map((x, j) =>
                      j === i ? { ...x, from_state: e.target.value } : x,
                    ),
                  )
                }
              >
                <option value="">{t('workflows.from')}</option>
                {opts.map((nm) => (
                  <option key={nm} value={nm}>
                    {nm}
                  </option>
                ))}
              </select>
              <span>→</span>
              <select
                value={tr.to_state}
                onChange={(e) =>
                  setTransitions((xs) =>
                    xs.map((x, j) =>
                      j === i ? { ...x, to_state: e.target.value } : x,
                    ),
                  )
                }
              >
                <option value="">{t('workflows.to')}</option>
                {opts.map((nm) => (
                  <option key={nm} value={nm}>
                    {nm}
                  </option>
                ))}
              </select>
            </div>
          )
        })}
        <button
          type="button"
          onClick={() =>
            setTransitions((xs) => [...xs, { from_state: '', to_state: '' }])
          }
        >
          {t('workflows.addTransition')}
        </button>

        {err && <p className="err">{err}</p>}
        <div className="row">
          <button type="submit" disabled={busy}>
            {busy ? t('workflows.creating') : t('workflows.create')}
          </button>
          <button
            type="button"
            className="btn--ghost"
            onClick={() => setShowCreate(false)}
          >
            {t('workflows.cancel')}
          </button>
        </div>
      </form>
      )}
    </section>
  )
}
