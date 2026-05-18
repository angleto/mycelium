import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import { useSession } from '../auth/useSession'
import type { components } from '../api/schema'

type Task = components['schemas']['TaskOut']
type Graph = components['schemas']['GraphOut']
type Dep = components['schemas']['DependencyOut']
type DepType = components['schemas']['DependencyType']

const TYPES: DepType[] = ['FS', 'SS', 'FF', 'SF']
const NW = 160
const NH = 44
const GX = 200
const GY = 64

// Naive longest-path layering: a DAG converges within |nodes| passes.
function layout(g: Graph): Map<string, { x: number; y: number }> {
  const level = new Map<string, number>()
  for (const n of g.nodes) level.set(n.id, 0)
  for (let pass = 0; pass < g.nodes.length; pass++) {
    let changed = false
    for (const e of g.edges) {
      const next = (level.get(e.predecessor) ?? 0) + 1
      if (next > (level.get(e.successor) ?? 0)) {
        level.set(e.successor, next)
        changed = true
      }
    }
    if (!changed) break
  }
  const perLevel = new Map<number, number>()
  const pos = new Map<string, { x: number; y: number }>()
  for (const n of g.nodes) {
    const lv = level.get(n.id) ?? 0
    const idx = perLevel.get(lv) ?? 0
    perLevel.set(lv, idx + 1)
    pos.set(n.id, { x: 20 + lv * GX, y: 20 + idx * GY })
  }
  return pos
}

export function GraphRoute() {
  const { t } = useTranslation()
  const session = useSession()
  const activeId = session?.workspaceId
  const [tasks, setTasks] = useState<Task[]>([])
  const [graph, setGraph] = useState<Graph | null>(null)
  const [deps, setDeps] = useState<Dep[]>([])
  const [pre, setPre] = useState('')
  const [suc, setSuc] = useState('')
  const [type, setType] = useState<DepType>('FS')
  const [lag, setLag] = useState(0)
  const [err, setErr] = useState<string | null>(null)

  const reload = useCallback(async () => {
    const h = workspaceHeader()
    const [g, d] = await Promise.all([
      api.GET('/graph', { params: { header: h } }),
      api.GET('/dependencies', { params: { header: h } }),
    ])
    if (g.data) setGraph(g.data)
    if (d.data) setDeps(d.data)
  }, [])

  useEffect(() => {
    let active = true
    void (async () => {
      const h = workspaceHeader()
      const [tk, g, d] = await Promise.all([
        api.GET('/tasks', { params: { header: h } }),
        api.GET('/graph', { params: { header: h } }),
        api.GET('/dependencies', { params: { header: h } }),
      ])
      if (!active) return
      if (tk.data) setTasks(tk.data)
      if (g.data) setGraph(g.data)
      if (d.data) setDeps(d.data)
    })()
    return () => {
      active = false
    }
  }, [activeId])

  const titleOf = (id: string) => tasks.find((x) => x.id === id)?.title ?? id.slice(0, 8)

  async function onAdd(e: FormEvent) {
    e.preventDefault()
    setErr(null)
    const { error } = await api.POST('/dependencies', {
      params: { header: workspaceHeader() },
      body: {
        predecessor_id: pre,
        successor_id: suc,
        type,
        lag_working_minutes: lag,
      },
    })
    if (error) {
      // A cycle is rejected (code dependency.cycle): shown verbatim.
      setErr(errMessage(error))
      return
    }
    await reload()
  }

  async function onRemove(id: string) {
    setErr(null)
    const { error } = await api.DELETE('/dependencies/{dependency_id}', {
      params: { header: workspaceHeader(), path: { dependency_id: id } },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    await reload()
  }

  if (!graph) return <p>{t('graph.loading')}</p>
  const pos = layout(graph)
  const maxX = Math.max(220, ...graph.nodes.map((n) => (pos.get(n.id)?.x ?? 0) + NW + 20))
  const maxY = Math.max(120, ...graph.nodes.map((n) => (pos.get(n.id)?.y ?? 0) + NH + 20))

  return (
    <section className="card">
      <h1>{t('graph.title')}</h1>

      <form onSubmit={(e) => void onAdd(e)} className="row">
        <label>
          {t('graph.predecessor')}
          <select required value={pre} onChange={(e) => setPre(e.target.value)}>
            <option value="">--</option>
            {tasks.map((x) => (
              <option key={x.id} value={x.id}>
                {x.title}
              </option>
            ))}
          </select>
        </label>
        <label>
          {t('graph.successor')}
          <select required value={suc} onChange={(e) => setSuc(e.target.value)}>
            <option value="">--</option>
            {tasks.map((x) => (
              <option key={x.id} value={x.id}>
                {x.title}
              </option>
            ))}
          </select>
        </label>
        <label>
          {t('graph.type')}
          <select value={type} onChange={(e) => setType(e.target.value as DepType)}>
            {TYPES.map((x) => (
              <option key={x} value={x}>
                {x}
              </option>
            ))}
          </select>
        </label>
        <label>
          {t('graph.lag')}
          <input
            type="number"
            value={lag}
            onChange={(e) => setLag(Number(e.target.value))}
          />
        </label>
        <button type="submit">{t('graph.add')}</button>
      </form>
      {err && <p className="err">{err}</p>}

      {graph.nodes.length === 0 ? (
        <p className="hint">{t('graph.empty')}</p>
      ) : (
        <svg
          className="dag"
          viewBox={`0 0 ${maxX} ${maxY}`}
          width="100%"
          role="img"
          aria-label={t('graph.title')}
        >
          {graph.edges.map((e, i) => {
            const a = pos.get(e.predecessor)
            const b = pos.get(e.successor)
            if (!a || !b) return null
            return (
              <g key={i}>
                <line
                  x1={a.x + NW}
                  y1={a.y + NH / 2}
                  x2={b.x}
                  y2={b.y + NH / 2}
                  className="dag__edge"
                />
                <text
                  x={(a.x + NW + b.x) / 2}
                  y={(a.y + b.y) / 2 + NH / 2 - 4}
                  className="dag__lbl"
                >
                  {e.type}
                </text>
              </g>
            )
          })}
          {graph.nodes.map((n) => {
            const p = pos.get(n.id)
            if (!p) return null
            return (
              <g key={n.id}>
                <rect
                  x={p.x}
                  y={p.y}
                  width={NW}
                  height={NH}
                  rx={6}
                  className={n.blocked ? 'dag__node dag__node--blocked' : 'dag__node'}
                />
                <text x={p.x + 8} y={p.y + 18} className="dag__title">
                  {n.title.slice(0, 20)}
                </text>
                <text x={p.x + 8} y={p.y + 34} className="dag__state">
                  {n.state}
                  {n.blocked ? ` · ${t('graph.blocked')}` : ''}
                </text>
              </g>
            )
          })}
        </svg>
      )}

      <h2>{t('graph.edges')}</h2>
      <ul className="list">
        {deps.map((d) => (
          <li key={d.id}>
            {titleOf(d.predecessor_id)} {'->'} {titleOf(d.successor_id)} ({d.type})
            <button type="button" onClick={() => void onRemove(d.id)}>
              {t('graph.remove')}
            </button>
          </li>
        ))}
      </ul>
    </section>
  )
}
