import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'

import { api, errMessage, workspaceHeader } from '../api/client'
import { useWorkflowStates } from '../lib/useWorkflowStates'
import type { components } from '../shared'
import { NotePickList } from './NotePickList'
import { TaskPickList } from './TaskPickList'

// One way to connect two things, notes or tasks (ADR-0077).
//
// The same panel on a note and on a task. Every connection the item has,
// from every mechanism (typed links, related tasks, subtasks, dependencies,
// mentions written in a body), grouped by meaning and said as a sentence
// with its direction: "Comes from X", "X comes from this". One Connect: pick
// the relation in words, then the note or task; one Disconnect on every row
// that can be removed. A mention changes when the text changes, and a
// promotion cannot be undone here, so their rows have no Disconnect and say
// why. The relations offered are the ones the server lists for this kind of
// item and this caller's permissions, so nothing on the screen is refused.

type ItemKind = components['schemas']['ItemKind']
type Connection = components['schemas']['ConnectionOut']
type Choice = components['schemas']['ConnectionChoice']
type Task = components['schemas']['TaskOut']
type Tag = components['schemas']['TagOut']

const DEP_TYPES = ['FS', 'SS', 'FF', 'SF'] as const

function choiceKey(c: Choice): string {
  return `${c.relation}:${c.direction}:${c.other_kind}`
}

function groupKey(c: Connection): string {
  return `${c.relation}.${c.direction}`
}

export function ConnectionsPanel({ kind, id }: { kind: ItemKind; id: string }) {
  const { t } = useTranslation()
  const [items, setItems] = useState<Connection[]>([])
  const [choices, setChoices] = useState<Choice[]>([])
  const [loaded, setLoaded] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [adding, setAdding] = useState<string>('')
  const [depType, setDepType] = useState<(typeof DEP_TYPES)[number]>('FS')
  const [lag, setLag] = useState('0')
  const [tasks, setTasks] = useState<Task[]>([])
  const [tags, setTags] = useState<Tag[]>([])
  const wfStates = useWorkflowStates()

  const load = useCallback(async () => {
    const { data, error } = await api.GET('/connections', {
      params: { header: workspaceHeader(), query: { this_kind: kind, this_id: id } },
    })
    if (error) {
      setErr(errMessage(error))
    } else {
      setErr(null)
      setItems(data?.items ?? [])
      setChoices(data?.choices ?? [])
    }
    setLoaded(true)
  }, [kind, id])

  useEffect(() => {
    let active = true
    void (async () => {
      if (active) await load()
    })()
    return () => {
      active = false
    }
  }, [load])

  const choice = choices.find((c) => choiceKey(c) === adding) ?? null

  // The task picker lists tasks, so it needs them; read only once someone
  // chooses a relation to a task.
  useEffect(() => {
    if (!choice || choice.other_kind !== 'task' || tasks.length > 0) return
    let active = true
    void (async () => {
      const [tk, tg] = await Promise.all([
        api.GET('/tasks', { params: { header: workspaceHeader() } }),
        api.GET('/tags', { params: { header: workspaceHeader() } }),
      ])
      if (!active) return
      if (tk.data) setTasks(tk.data)
      if (tg.data) setTags(tg.data)
    })()
    return () => {
      active = false
    }
  }, [choice, tasks.length])

  const groups = useMemo(() => {
    const out: { key: string; rows: Connection[] }[] = []
    for (const c of items) {
      const k = groupKey(c)
      const g = out.find((x) => x.key === k)
      if (g) g.rows.push(c)
      else out.push({ key: k, rows: [c] })
    }
    return out
  }, [items])

  const connected = useMemo(() => new Set(items.map((c) => c.other_id)), [items])

  async function connect(otherId: string) {
    if (!choice) return
    setBusy(true)
    setErr(null)
    const { error } = await api.POST('/connections/connect', {
      params: { header: workspaceHeader() },
      body: {
        this_kind: kind,
        this_id: id,
        relation: choice.relation,
        direction: choice.direction,
        that_kind: choice.other_kind,
        that_id: otherId,
        dependency_type: choice.relation === 'depends_on' ? depType : null,
        lag_minutes: choice.relation === 'depends_on' ? Number(lag) || 0 : null,
      },
    })
    setBusy(false)
    if (error) {
      setErr(errMessage(error))
      return
    }
    setAdding('')
    await load()
  }

  async function disconnect(c: Connection) {
    setBusy(true)
    setErr(null)
    const { error } = await api.POST('/connections/disconnect', {
      params: { header: workspaceHeader() },
      body: { this_kind: kind, this_id: id, handle: c.handle, that_kind: c.other_kind },
    })
    setBusy(false)
    if (error) {
      setErr(errMessage(error))
      return
    }
    await load()
  }

  function detail(c: Connection): string | null {
    if (c.relation !== 'depends_on') return null
    const d = c.detail as { type?: string; lag_minutes?: number; holding?: boolean } | undefined
    const lagMin = d?.lag_minutes ?? 0
    // ``holding`` comes from the server's readiness rule: the waiting task
    // cannot start yet because of this dependency.
    const holding = d?.holding ? t(`itemConnections.holding.${c.direction === 'in' ? 'in' : 'out'}`) : null
    return [
      t(`itemConnections.depType.${d?.type ?? 'FS'}`),
      lagMin ? t('itemConnections.lag', { minutes: lagMin }) : null,
      holding,
    ]
      .filter(Boolean)
      .join(' · ')
  }

  return (
    <section className="connections" aria-labelledby={`conn-${id}`}>
      <h2 id={`conn-${id}`} className="connections__title">
        {t('itemConnections.title')}
      </h2>
      {err && <p className="error">{err}</p>}
      {!loaded ? (
        <p className="hint">{t('common.loading')}</p>
      ) : groups.length === 0 ? (
        <p className="hint">{t(`itemConnections.empty.${kind}`)}</p>
      ) : (
        groups.map((g) => (
          <div key={g.key} className="connections__group">
            <h3 className="connections__grouphead">
              {t(`itemConnections.group.${g.key}`)}
            </h3>
            <ul className="connections__list">
              {g.rows.map((c) => (
                <li key={c.handle} className="connections__row">
                  <span className="chip">{t(`itemConnections.kind.${c.other_kind}`)}</span>
                  <Link
                    to={c.other_kind === 'note' ? `/notes/${c.other_id}` : `/tasks/${c.other_id}`}
                    className="connections__name"
                  >
                    {c.other_title || t('itemConnections.untitled')}
                  </Link>
                  {detail(c) && <span className="muted">{detail(c)}</span>}
                  {c.removable ? (
                    <button
                      type="button"
                      className="btn--ghost btn--sm"
                      disabled={busy}
                      onClick={() => void disconnect(c)}
                    >
                      {t('itemConnections.disconnect')}
                    </button>
                  ) : (
                    <span className="hint">
                      {t(
                        c.relation === 'mentions'
                          ? 'itemConnections.fixed.mentions'
                          : 'itemConnections.fixed.promoted',
                      )}
                    </span>
                  )}
                </li>
              ))}
            </ul>
          </div>
        ))
      )}

      {choices.length > 0 && (
        <div className="connections__add">
          <label className="connections__addrow">
            <span>{t('itemConnections.connect')}</span>
            <select value={adding} onChange={(e) => setAdding(e.target.value)}>
              <option value="">{t('itemConnections.choose')}</option>
              {choices.map((c) => (
                <option key={choiceKey(c)} value={choiceKey(c)}>
                  {t(`itemConnections.choice.${kind}.${c.relation}.${c.direction}.${c.other_kind}`)}
                </option>
              ))}
            </select>
          </label>
          {choice?.relation === 'depends_on' && (
            <div className="connections__addrow">
              <label>
                <span>{t('itemConnections.depTypeLabel')}</span>
                <select
                  value={depType}
                  onChange={(e) => setDepType(e.target.value as (typeof DEP_TYPES)[number])}
                >
                  {DEP_TYPES.map((d) => (
                    <option key={d} value={d}>
                      {t(`itemConnections.depType.${d}`)}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                <span>{t('itemConnections.lagLabel')}</span>
                <input
                  type="number"
                  step={15}
                  value={lag}
                  onChange={(e) => setLag(e.target.value)}
                  className="connections__lag"
                />
              </label>
            </div>
          )}
          {choice && (
            <div className="connections__picker">
              {choice.other_kind === 'note' ? (
                <NotePickList
                  exclude={kind === 'note' ? [id, ...connected] : [...connected]}
                  value={null}
                  onPick={(otherId) => void connect(otherId)}
                  placeholder={t('itemConnections.pickNote')}
                />
              ) : (
                <TaskPickList
                  tasks={tasks.filter((x) => x.id !== id && !connected.has(x.id))}
                  tags={tags}
                  states={wfStates}
                  value={null}
                  onPick={(otherId) => void connect(otherId)}
                  placeholder={t('itemConnections.pickTask')}
                />
              )}
            </div>
          )}
        </div>
      )}
    </section>
  )
}
