import { useCallback, useEffect, useMemo, useState, type CSSProperties } from 'react'
import { useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import { useSession } from '../auth/useSession'
import { kindGlyph } from '../lib/tagGlyph'
import { readableOn } from '../lib/color'
import { useFocus } from '../lib/focus'
import { useWorkflowStates } from '../lib/useWorkflowStates'
import { formatDueDate } from '../lib/time'
import type { components } from '../shared'

type Task = components['schemas']['TaskOut']
type Graph = components['schemas']['GraphOut']
type Dep = components['schemas']['DependencyOut']
type DepType = components['schemas']['DependencyType']
type Tag = components['schemas']['TagOut']

const ORDER: DepType[] = ['FS', 'SS', 'FF', 'SF']
const NW = 170
const NH = 46
const GX = 210
const GY = 70

function layout(ids: string[], edges: Graph['edges']): Map<string, { x: number; y: number }> {
  const level = new Map<string, number>()
  for (const id of ids) level.set(id, 0)
  for (let pass = 0; pass < ids.length; pass++) {
    let changed = false
    for (const e of edges) {
      if (!level.has(e.predecessor) || !level.has(e.successor)) continue
      const next = (level.get(e.predecessor) ?? 0) + 1
      if (next > (level.get(e.successor) ?? 0)) {
        level.set(e.successor, next)
        changed = true
      }
    }
    if (!changed) break
  }
  const per = new Map<number, number>()
  const pos = new Map<string, { x: number; y: number }>()
  for (const id of ids) {
    const lv = level.get(id) ?? 0
    const idx = per.get(lv) ?? 0
    per.set(lv, idx + 1)
    pos.set(id, { x: 20 + lv * GX, y: 20 + idx * GY })
  }
  return pos
}

export function GraphRoute() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const session = useSession()
  const activeId = session?.workspaceId
  const { focusIds, active: focusActive, clientId, projectId } = useFocus()
  // When a client/project focus is set, the tag catalog must be
  // scoped to it (the server filters by scope); otherwise tags from
  // other clients/projects leak into the filter list.
  const tagQuery = useMemo(
    () =>
      clientId
        ? {
            for_client: clientId,
            ...(projectId ? { for_project: projectId } : {}),
          }
        : undefined,
    [clientId, projectId],
  )
  const [tasks, setTasks] = useState<Task[]>([])
  const [tags, setTags] = useState<Tag[]>([])
  const [graph, setGraph] = useState<Graph | null>(null)
  const [deps, setDeps] = useState<Dep[]>([])
  const [scope, setScope] = useState<'all' | 'mine' | 'ai'>('all')
  const [tagFilter, setTagFilter] = useState<Set<string>>(new Set())
  const [zoom, setZoom] = useState(1)
  const [from, setFrom] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  // Workflow states drive both the per-state filter and the
  // hide-terminal default (a finished task shouldn't clutter the DAG
  // by default — the toggle reveals them, mirrors /tasks).
  const wfStates = useWorkflowStates()
  const [stateFilter, setStateFilter] = useState<Set<string>>(new Set())
  const [showTerminal, setShowTerminal] = useState(false)

  const reload = useCallback(async () => {
    const h = workspaceHeader()
    const [tk, g, d, tg] = await Promise.all([
      api.GET('/tasks', { params: { header: h } }),
      api.GET('/graph', { params: { header: h } }),
      api.GET('/dependencies', { params: { header: h } }),
      api.GET('/tags', { params: { header: h, query: tagQuery } }),
    ])
    if (tk.data) setTasks(tk.data)
    if (g.data) setGraph(g.data)
    if (d.data) setDeps(d.data)
    if (tg.data) setTags(tg.data)
  }, [tagQuery])

  useEffect(() => {
    let active = true
    void (async () => {
      const h = workspaceHeader()
      const [tk, g, d, tg] = await Promise.all([
        api.GET('/tasks', { params: { header: h } }),
        api.GET('/graph', { params: { header: h } }),
        api.GET('/dependencies', { params: { header: h } }),
        api.GET('/tags', { params: { header: h, query: tagQuery } }),
      ])
      if (!active) return
      if (tk.data) setTasks(tk.data)
      if (g.data) setGraph(g.data)
      if (d.data) setDeps(d.data)
      if (tg.data) setTags(tg.data)
    })()
    return () => {
      active = false
    }
  }, [activeId, tagQuery])

  const taskById = new Map(tasks.map((x) => [x.id, x]))
  const titleOf = (id: string) => taskById.get(id)?.title ?? id.slice(0, 8)
  // Filter chips come from the tag catalog (authoritative + complete),
  // not from whatever tags happen to be on the loaded tasks (that was
  // empty/wrong, especially with a focus or an empty task list).
  const allTags = tags.filter((g) => g.status !== 'archived')
  const terminalIds = new Set(
    wfStates.filter((s) => s.is_terminal).map((s) => s.id),
  )

  function visible(id: string): boolean {
    const tk = taskById.get(id)
    if (scope === 'mine' && tk?.executor_kind !== 'human') return false
    if (scope === 'ai' && tk?.executor_kind !== 'llm_agent') return false
    // Terminal-state tasks are hidden by default (the toggle reveals
    // them, mirroring /tasks). A node with no state info is shown.
    if (!showTerminal && tk && terminalIds.has(tk.state_id)) return false
    // Per-state filter: when one or more states are selected, only
    // tasks in those states show.
    if (stateFilter.size > 0 && tk && !stateFilter.has(tk.state_id)) {
      return false
    }
    // Multi-tag: OR — show a task if it carries ANY selected tag (no
    // selection = all). AND made "select all tags" hide everything,
    // since no single task has every tag.
    if (
      tagFilter.size > 0 &&
      !(tk?.tags ?? []).some((g) => tagFilter.has(g.id))
    ) {
      return false
    }
    // Project/client focus (sidebar).
    if (
      focusActive &&
      !(tk?.tags ?? []).some((g) => focusIds.includes(g.id))
    ) {
      return false
    }
    return true
  }

  async function onNodeClick(id: string) {
    if (from === null) {
      setFrom(id)
      return
    }
    if (from === id) {
      setFrom(null)
      return
    }
    setErr(null)
    const { error } = await api.POST('/dependencies', {
      params: { header: workspaceHeader() },
      body: {
        predecessor_id: from,
        successor_id: id,
        type: 'FS',
        lag_working_minutes: 0,
      },
    })
    setFrom(null)
    if (error) {
      setErr(errMessage(error))
      return
    }
    await reload()
  }

  async function cycleType(predId: string, succId: string, cur: string) {
    const dep = deps.find(
      (d) => d.predecessor_id === predId && d.successor_id === succId,
    )
    if (!dep) return
    const next = ORDER[(ORDER.indexOf(cur as DepType) + 1) % ORDER.length]
    setErr(null)
    const del = await api.DELETE('/dependencies/{dependency_id}', {
      params: { header: workspaceHeader(), path: { dependency_id: dep.id } },
    })
    if (del.error) {
      setErr(errMessage(del.error))
      return
    }
    const add = await api.POST('/dependencies', {
      params: { header: workspaceHeader() },
      body: {
        predecessor_id: predId,
        successor_id: succId,
        type: next,
        lag_working_minutes: 0,
      },
    })
    if (add.error) {
      setErr(errMessage(add.error))
      return
    }
    await reload()
  }

  async function removeDep(depId: string) {
    setErr(null)
    const { error } = await api.DELETE('/dependencies/{dependency_id}', {
      params: { header: workspaceHeader(), path: { dependency_id: depId } },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    await reload()
  }

  if (!graph) return <p>{t('graph.loading')}</p>

  const nodes = graph.nodes.filter((n) => visible(n.id))
  const ids = nodes.map((n) => n.id)
  const idset = new Set(ids)
  const edges = graph.edges.filter(
    (e) => idset.has(e.predecessor) && idset.has(e.successor),
  )
  const pos = layout(ids, edges)
  const maxX = Math.max(240, ...ids.map((i) => (pos.get(i)?.x ?? 0) + NW + 20))
  const maxY = Math.max(140, ...ids.map((i) => (pos.get(i)?.y ?? 0) + NH + 20))

  return (
    <section className="card">
      <h1>{t('graph.title')}</h1>
      <p className="hint">{t('graph.connectHint')}</p>

      <div className="row">
        <label>
          {t('graph.scopeLabel')}
          <select
            value={scope}
            onChange={(e) => setScope(e.target.value as 'all' | 'mine' | 'ai')}
          >
            <option value="all">{t('graph.scopeAll')}</option>
            <option value="mine">{t('graph.scopeMine')}</option>
            <option value="ai">{t('graph.scopeAi')}</option>
          </select>
        </label>
        <button
          type="button"
          role="switch"
          aria-checked={showTerminal}
          className={
            'toggle-pill' + (showTerminal ? ' toggle-pill--on' : '')
          }
          onClick={() => setShowTerminal((v) => !v)}
        >
          {t('graph.showTerminal')}:{' '}
          {showTerminal ? t('common.on') : t('common.off')}
        </button>
      </div>
      {wfStates.length > 0 && (
        <div className="tagfilter">
          <span className="muted">{t('graph.stateLabel')}:</span>
          {wfStates.map((s) => {
            const on = stateFilter.has(s.id)
            return (
              <button
                key={s.id}
                type="button"
                aria-pressed={on}
                className={'chip ' + (on ? 'chip--on' : 'chip--off')}
                onClick={() =>
                  setStateFilter((prev) => {
                    const n = new Set(prev)
                    if (n.has(s.id)) n.delete(s.id)
                    else n.add(s.id)
                    return n
                  })
                }
              >
                {on ? '✓ ' : ''}
                {s.name}
              </button>
            )
          })}
          {stateFilter.size > 0 && (
            <button
              type="button"
              className="btn--ghost btn--sm"
              onClick={() => setStateFilter(new Set())}
            >
              {t('graph.clearStates')}
            </button>
          )}
        </div>
      )}
      <div className="tagfilter">
        <span className="muted">{t('graph.tagsLabel')}:</span>
        {allTags.length === 0 ? (
          <span className="hint">{t('graph.noTags')}</span>
        ) : (
          allTags.map((g) => {
            const on = tagFilter.has(g.id)
            // Color comes from the tag itself. When ON the colour fills
            // the chip (with strong opacity); when OFF only the dot +
            // border carry the colour, so the chip stays visually
            // distinguishable across same-named tags on different
            // clients/projects.
            const color = g.color || undefined
            const style: CSSProperties = on
              ? color
                ? {
                    background: color,
                    borderColor: color,
                    color: readableOn(color),
                  }
                : {}
              : color
                ? { borderColor: `${color}66` }
                : {}
            return (
              <button
                key={g.id}
                type="button"
                aria-pressed={on}
                className={
                  'chip ' + (on ? 'chip--on' : 'chip--off')
                }
                style={style}
                title={`${g.kind}: ${g.name}`}
                onClick={() =>
                  setTagFilter((s) => {
                    const n = new Set(s)
                    if (n.has(g.id)) n.delete(g.id)
                    else n.add(g.id)
                    return n
                  })
                }
              >
                <span
                  className="chip__glyph"
                  style={{
                    // The glyph always uses the chip's computed
                    // foreground: ON it would otherwise melt into the
                    // tag-color fill; OFF a near-surface raw tag color
                    // would be invisible on the chip body. The dot +
                    // `${color}66` border already carry the hue cue.
                    color: 'currentColor',
                  }}
                  aria-hidden="true"
                >
                  {kindGlyph(g.kind)}
                </span>
                {g.name}
              </button>
            )
          })
        )}
        {tagFilter.size > 0 && (
          <button
            type="button"
            className="btn--ghost btn--sm"
            onClick={() => setTagFilter(new Set())}
          >
            {t('graph.clearTags')}
          </button>
        )}
      </div>
      {err && <p className="err">{err}</p>}

      {nodes.length === 0 ? (
        <p className="hint">{t('graph.empty')}</p>
      ) : (
        <>
        <div className="dagbar">
          <button
            type="button"
            className="btn--ghost btn--sm"
            onClick={() => setZoom((z) => Math.max(0.4, z - 0.2))}
          >
            −
          </button>
          <button
            type="button"
            className="btn--ghost btn--sm"
            onClick={() => setZoom(1)}
          >
            {Math.round(zoom * 100)}%
          </button>
          <button
            type="button"
            className="btn--ghost btn--sm"
            onClick={() => setZoom((z) => Math.min(2.5, z + 0.2))}
          >
            +
          </button>
          <span className="muted dag__legend">
            FS {t('graph.depFS')} · SS {t('graph.depSS')} · FF{' '}
            {t('graph.depFF')} · SF {t('graph.depSF')}
          </span>
        </div>
        <div className="dagwrap">
        <svg
          className="dag"
          viewBox={`0 0 ${maxX} ${maxY}`}
          width={maxX * zoom}
          height={maxY * zoom}
          role="img"
          aria-label={t('graph.title')}
        >
          {edges.map((e, i) => {
            const a = pos.get(e.predecessor)
            const b = pos.get(e.successor)
            if (!a || !b) return null
            const mx = (a.x + NW + b.x) / 2
            const my = (a.y + b.y) / 2 + NH / 2
            return (
              <g key={i}>
                <line
                  x1={a.x + NW}
                  y1={a.y + NH / 2}
                  x2={b.x}
                  y2={b.y + NH / 2}
                  className="dag__edge"
                />
                <rect
                  x={mx - 15}
                  y={my - 17}
                  width={30}
                  height={18}
                  rx={4}
                  className="dag__lblbg dag__lbl--btn"
                  onClick={() =>
                    void cycleType(e.predecessor, e.successor, e.type)
                  }
                >
                  <title>{t(`graph.dep${e.type}`)}</title>
                </rect>
                <text
                  x={mx}
                  y={my - 8}
                  className="dag__lbl dag__lbl--btn"
                  onClick={() =>
                    void cycleType(e.predecessor, e.successor, e.type)
                  }
                >
                  <title>{t(`graph.dep${e.type}`)}</title>
                  {e.type}
                </text>
              </g>
            )
          })}
          {nodes.map((n) => {
            const p = pos.get(n.id)
            if (!p) return null
            const sel = from === n.id
            const tk = taskById.get(n.id)
            // SVG <title> is rendered by the browser as a native hover
            // tooltip — gives full text on a truncated title without
            // forcing the user to click and lose their place in /graph.
            const tagsLine = (tk?.tags ?? [])
              .map((g) => `${g.kind}:${g.name}`)
              .join(', ')
            const tooltipLines = [
              n.title,
              tk?.state ? `state: ${tk.state}` : '',
              tk?.priority != null ? `priority: ${tk.priority}` : '',
              tk?.due_date ? `due: ${formatDueDate(tk.due_date)}` : '',
              tagsLine ? `tags: ${tagsLine}` : '',
              tk?.executor_kind === 'llm_agent' ? 'AI agent' : '',
            ].filter(Boolean)
            const tooltip = tooltipLines.join('\n')
            return (
              <g key={n.id}>
                <title>{tooltip}</title>
                <rect
                  x={p.x}
                  y={p.y}
                  width={NW}
                  height={NH}
                  rx={7}
                  className={
                    'dag__node dag__node--btn' +
                    (sel ? ' dag__node--sel' : n.blocked ? ' dag__node--blocked' : '')
                  }
                  onClick={() => void onNodeClick(n.id)}
                />
                <text
                  x={p.x + 9}
                  y={p.y + 19}
                  className="dag__title dag__title--link"
                  onClick={() => navigate(`/tasks/${n.id}`)}
                >
                  {n.title.slice(0, 20)}
                </text>
                <text x={p.x + 9} y={p.y + 36} className="dag__state">
                  {n.state}
                  {tk?.executor_kind === 'llm_agent' ? ' · AI' : ''}
                </text>
              </g>
            )
          })}
        </svg>
        </div>
        </>
      )}

      <h2>{t('graph.edges')}</h2>
      <ul className="list">
        {deps
          .filter(
            (d) => idset.has(d.predecessor_id) && idset.has(d.successor_id),
          )
          .map((d) => (
            <li key={d.id}>
              {titleOf(d.predecessor_id)}{' '}
              <button
                type="button"
                className="btn--ghost btn--sm"
                onClick={() =>
                  void cycleType(d.predecessor_id, d.successor_id, d.type)
                }
              >
                {t(`graph.dep${d.type}`)}
              </button>{' '}
              {titleOf(d.successor_id)}
              <button
                type="button"
                className="btn--ghost btn--sm"
                onClick={() => void removeDep(d.id)}
              >
                {t('graph.remove')}
              </button>
            </li>
          ))}
      </ul>
    </section>
  )
}
