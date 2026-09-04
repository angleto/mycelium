import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api, authFetch, errMessage, workspaceHeader } from '../api/client'
import { useSession } from '../auth/useSession'
import type { components } from '../shared'
import { downloadResponse, sanitizeFilename } from '../lib/downloadFile'

type Workflow = components['schemas']['WorkflowOut']
type State = components['schemas']['StateOut']
type Edge = components['schemas']['TransitionOut']
type WfMeta = { states: State[]; edges: Edge[] }
type StateRow = {
  name: string
  is_initial: boolean
  is_terminal: boolean
  is_hidden: boolean
  description?: string | null
}
type Transition = { from_state: string; to_state: string }
type EditRow = StateRow & { id?: string }
type Edit = {
  id: string
  name: string
  description: string
  states: EditRow[]
  tr: Transition[]
}

// A workflow document is a few kilobytes of JSON. Anything past this is
// not one, and shipping it to the server to find out is pointless.
const MAX_IMPORT_BYTES = 1_000_000

// WorkflowDefinition editor. The backend enforces "exactly one initial
// state" (workflow.invalid) and the transition rules; errors surface
// here verbatim from the i18n catalog.
//
// Export and Import are SERVER operations (docs/adr/0052): this route
// posts the file and renders the answer, and every rule about what a
// valid document is lives in ``services/workflow_io.py``. The SPA has
// no parser of its own on purpose -- ``mycelium workflow import`` is
// the other client, and two parsers would be two answers.
//
// The consequence is worth stating where the buttons are: Import
// WRITES. There is no "press Save to apply" step, which is why it asks
// first. Export downloads the SAVED workflow, so unsaved edits in the
// panel above are not in the file.
export function WorkflowsRoute() {
  const { t } = useTranslation()
  const session = useSession()
  const activeId = session?.workspaceId
  const [list, setList] = useState<Workflow[]>([])
  const [meta, setMeta] = useState<Record<string, WfMeta>>({})
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  // New-workflow template mirrors the system default
  // (todo -> in_progress -> done) so it is not misleading.
  const [states, setStates] = useState<StateRow[]>([
    { name: 'todo', is_initial: true, is_terminal: false, is_hidden: false },
    { name: 'in_progress', is_initial: false, is_terminal: false, is_hidden: false },
    { name: 'done', is_initial: false, is_terminal: true, is_hidden: false },
  ])
  const [transitions, setTransitions] = useState<Transition[]>([
    { from_state: 'todo', to_state: 'in_progress' },
    { from_state: 'in_progress', to_state: 'done' },
    { from_state: 'in_progress', to_state: 'todo' },
    { from_state: 'done', to_state: 'in_progress' },
  ])
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [msg, setMsg] = useState<string | null>(null)
  const [editing, setEditing] = useState<Edit | null>(null)
  const [showCreate, setShowCreate] = useState(false)
  const editImport = useRef<HTMLInputElement>(null)
  const createImport = useRef<HTMLInputElement>(null)

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
    // An editor left open on a workflow that is no longer in the list
    // (it was just deleted) is broken state twice over: Save would
    // PATCH a row that is gone, and its panel -- one of the two
    // surfaces this route renders err/msg on -- is no longer mounted,
    // so the next failure would have nowhere to show.
    setEditing((e) => (e && !data.some((w) => w.id === e.id) ? null : e))
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

  // The file never gets parsed here: the server owns the rules, and
  // a second reading of them in the SPA would be a second answer to
  // "is this document valid" (docs/adr/0052).
  async function readDocument(file: File): Promise<unknown | null> {
    setErr(null)
    setMsg(null)
    if (file.size > MAX_IMPORT_BYTES) {
      setErr(t('workflows.importErrTooLarge'))
      return null
    }
    try {
      return JSON.parse(await file.text()) as unknown
    } catch {
      setErr(t('workflows.importErrNotJson'))
      return null
    }
  }

  async function exportWorkflow(w: Workflow) {
    setErr(null)
    setMsg(null)
    const res = await authFetch(`/workflows/${w.id}/export`)
    if (!res.ok) {
      setErr(errMessage(await res.json().catch(() => null)))
      return
    }
    await downloadResponse(res, `workflow-${sanitizeFilename(w.name)}.json`)
  }

  async function importIntoWorkflow(w: Workflow, file: File) {
    // Unlike Save, this one cannot be walked back with Cancel.
    if (!window.confirm(t('workflows.confirmImport', { name: w.name }))) return
    const payload = await readDocument(file)
    if (payload === null) return
    setBusy(true)
    const res = await authFetch(`/workflows/${w.id}/import`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    setBusy(false)
    if (!res.ok) {
      setErr(errMessage(await res.json().catch(() => null)))
      return
    }
    setEditing(null)
    setMsg(t('workflows.imported', { name: w.name }))
    await load()
  }

  async function importAsNewWorkflow(file: File) {
    const payload = await readDocument(file)
    if (payload === null) return
    setBusy(true)
    const res = await authFetch('/workflows/import', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    setBusy(false)
    if (!res.ok) {
      setErr(errMessage(await res.json().catch(() => null)))
      return
    }
    const created = (await res.json()) as Workflow
    setShowCreate(false)
    setMsg(t('workflows.imported', { name: created.name }))
    await load()
  }

  async function onCreate(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setErr(null)
    setMsg(null)
    const { error } = await api.POST('/workflows', {
      params: { header: workspaceHeader() },
      body: {
        name,
        description: description || null,
        states: states.map((s, i) => ({
          name: s.name,
          ord: i,
          is_initial: s.is_initial,
          is_terminal: s.is_terminal,
          is_hidden: s.is_hidden,
          description: s.description || null,
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
      description: w.description ?? '',
      states: [...m.states]
        .sort((a, b) => a.ord - b.ord)
        .map((s) => ({
          id: s.id,
          name: s.name,
          is_initial: s.is_initial,
          is_terminal: s.is_terminal,
          // ``is_hidden`` is optional in the OpenAPI schema (default
          // false at the DB level, defaulted to ?: in TS); coerce so
          // EditRow's strict bool stays consistent.
          is_hidden: s.is_hidden ?? false,
          description: s.description ?? '',
        })),
      tr: m.edges.map((e) => ({
        from_state: nameById.get(e.from_state_id) ?? '',
        to_state: nameById.get(e.to_state_id) ?? '',
      })),
    })
    setErr(null)
    setMsg(null)
  }

  function patchE(p: Partial<Edit>) {
    setEditing((e) => (e ? { ...e, ...p } : e))
  }

  async function saveEdit() {
    if (!editing) return
    setBusy(true)
    setErr(null)
    setMsg(null)
    const { error } = await api.PATCH('/workflows/{workflow_id}', {
      params: { header: workspaceHeader(), path: { workflow_id: editing.id } },
      body: {
        name: editing.name,
        description: editing.description || null,
        states: editing.states.map((s, i) => ({
          id: s.id,
          name: s.name,
          ord: i,
          is_initial: s.is_initial,
          is_terminal: s.is_terminal,
          is_hidden: s.is_hidden,
          description: s.description || null,
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
    setMsg(null)
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
    setMsg(null)
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
                        {s.is_initial ? ` · ${t('workflows.defaultState')}` : ''}
                        {s.is_terminal ? ` · ${t('workflows.terminal')}` : ''}
                        {s.is_hidden ? ` · ${t('workflows.hidden')}` : ''}
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
                    <label>
                      {t('workflows.description')}
                      <textarea
                        rows={2}
                        value={editing.description}
                        placeholder={t('workflows.descriptionHint')}
                        onChange={(e) => patchE({ description: e.target.value })}
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
                            type="radio"
                            name="wf-edit-default"
                            checked={s.is_initial}
                            onChange={() =>
                              patchE({
                                states: editing.states.map((x, j) => ({
                                  ...x,
                                  is_initial: j === i,
                                })),
                              })
                            }
                          />{' '}
                          {t('workflows.defaultState')}
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
                        <label title={t('workflows.hiddenHint')}>
                          <input
                            type="checkbox"
                            checked={s.is_hidden}
                            onChange={(e) =>
                              patchE({
                                states: editing.states.map((x, j) =>
                                  j === i
                                    ? { ...x, is_hidden: e.target.checked }
                                    : x,
                                ),
                              })
                            }
                          />{' '}
                          {t('workflows.hidden')}
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
                          onClick={() => {
                            // Also prune transitions that reference
                            // the removed state's name; otherwise the
                            // backend's transition validator raises
                            // WORKFLOW_INVALID with the misleading
                            // "exactly one initial state" message.
                            const goneName = editing.states[i].name
                            patchE({
                              states: editing.states.filter(
                                (_, j) => j !== i,
                              ),
                              tr: editing.tr.filter(
                                (x) =>
                                  x.from_state !== goneName &&
                                  x.to_state !== goneName,
                              ),
                            })
                          }}
                        >
                          ✕
                        </button>
                        <input
                          type="text"
                          placeholder={t('workflows.stateDescriptionHint')}
                          title={t('workflows.stateDescriptionHint')}
                          value={s.description ?? ''}
                          onChange={(e) =>
                            patchE({
                              states: editing.states.map((x, j) =>
                                j === i
                                  ? { ...x, description: e.target.value }
                                  : x,
                              ),
                            })
                          }
                          style={{ flex: 1, minWidth: '12rem' }}
                        />
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
                              is_hidden: false,
                              description: '',
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
                        onClick={() => {
                          setEditing(null)
                          setErr(null)
                          setMsg(null)
                        }}
                      >
                        {t('workflows.cancel')}
                      </button>
                      <button
                        type="button"
                        className="btn--ghost"
                        title={t('workflows.exportHint')}
                        onClick={() => void exportWorkflow(w)}
                      >
                        {t('workflows.export')}
                      </button>
                      <button
                        type="button"
                        className="btn--ghost"
                        disabled={busy}
                        title={t('workflows.importHint')}
                        onClick={() => editImport.current?.click()}
                      >
                        {t('workflows.import')}
                      </button>
                      <input
                        ref={editImport}
                        type="file"
                        accept="application/json,.json"
                        hidden
                        onChange={(ev) => {
                          const f = ev.target.files?.[0]
                          ev.target.value = ''
                          if (f) void importIntoWorkflow(w, f)
                        }}
                      />
                    </div>
                    {err && <p className="err">{err}</p>}
                    {msg && <p className="ok">{msg}</p>}
                  </div>
                )}
              </li>
            )
          })}
        </ul>
      )}
      {/* err and msg get exactly one surface, so a message can neither
          be shown twice nor land somewhere the user is not looking: the
          open editor panel first, then the create form, then here. The
          panel is guaranteed to be mounted whenever `editing` is set,
          because `load` drops an editor whose workflow has gone. */}
      {!editing && !showCreate && err && <p className="err">{err}</p>}
      {!editing && !showCreate && msg && <p className="ok">{msg}</p>}

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
        <label>
          {t('workflows.description')}
          <textarea
            rows={2}
            value={description}
            placeholder={t('workflows.descriptionHint')}
            onChange={(e) => setDescription(e.target.value)}
          />
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
                type="radio"
                name="wf-new-default"
                checked={s.is_initial}
                onChange={() =>
                  setStates((rs) =>
                    rs.map((r, j) => ({ ...r, is_initial: j === i })),
                  )
                }
              />{' '}
              {t('workflows.defaultState')}
            </label>
            <label>
              <input
                type="checkbox"
                checked={s.is_terminal}
                onChange={(e) => setState(i, { is_terminal: e.target.checked })}
              />{' '}
              {t('workflows.terminal')}
            </label>
            <label title={t('workflows.hiddenHint')}>
              <input
                type="checkbox"
                checked={s.is_hidden}
                onChange={(e) => setState(i, { is_hidden: e.target.checked })}
              />{' '}
              {t('workflows.hidden')}
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
            setStates((rs) => [
              ...rs,
              { name: '', is_initial: false, is_terminal: false, is_hidden: false },
            ])
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

        {!editing && err && <p className="err">{err}</p>}
        {!editing && msg && <p className="ok">{msg}</p>}
        <div className="row">
          <button type="submit" disabled={busy}>
            {busy ? t('workflows.creating') : t('workflows.create')}
          </button>
          <button
            type="button"
            className="btn--ghost"
            onClick={() => {
              setShowCreate(false)
              setErr(null)
              setMsg(null)
            }}
          >
            {t('workflows.cancel')}
          </button>
          <button
            type="button"
            className="btn--ghost"
            disabled={busy}
            title={t('workflows.importNewHint')}
            onClick={() => createImport.current?.click()}
          >
            {t('workflows.import')}
          </button>
          <input
            ref={createImport}
            type="file"
            accept="application/json,.json"
            hidden
            onChange={(ev) => {
              const f = ev.target.files?.[0]
              ev.target.value = ''
              if (f) void importAsNewWorkflow(f)
            }}
          />
        </div>
      </form>
      )}
    </section>
  )
}
