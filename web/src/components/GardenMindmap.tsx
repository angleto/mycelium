/**
 * Garden mindmap (ADR-0029 P2, task b9ada22).
 *
 * Fourth tab of the garden: every alive note in scope is a node,
 * every typed note-to-note link is a solid edge. Tags shared between
 * notes (kind=generic only — client/project would just hairball the
 * graph) become opt-in dashed edges. Drag from a node's edge handle
 * to another node to create a manual link; click an existing manual
 * edge to delete it. Tag-derived edges are virtual and not editable.
 *
 * Layout: deterministic seed (cluster-by-primary-tag + ring for the
 * unlinked) on first mount; user positions persist to localStorage
 * per workspace, so the graph stays put across reloads.
 */

import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from 'react'
import {
  Background,
  Controls,
  Handle,
  MarkerType,
  MiniMap,
  Position,
  ReactFlow,
  ReactFlowProvider,
  useEdgesState,
  useNodesState,
  useReactFlow,
  type Connection,
  type Edge,
  type EdgeMouseHandler,
  type Node,
  type NodeMouseHandler,
  type NodeProps,
} from '@xyflow/react'
import '@xyflow/react/dist/base.css'
import { useTranslation } from 'react-i18next'

import { authFetch } from '../api/client'
import type { components } from '../api/schema'

type Note = components['schemas']['NoteOut']
type TagBrief = components['schemas']['TagBrief']

type LinkKind = 'atom_of' | 'references' | 'replies_to' | 'supersedes'
const LINK_KINDS: LinkKind[] = ['references', 'atom_of', 'replies_to', 'supersedes']

interface WorkspaceLink {
  id: string
  parent_note_id: string
  child_note_id: string
  kind: string
  created_at: string
}

const MATURITY_GLYPH: Record<string, string> = {
  seed: '🌱',
  growing: '🌿',
  mature: '🌳',
  dormant: '🍂',
}

// Forest palette: each edge kind takes a natural metaphor.
// references = a passing scent (muted dashed); atom_of = a solid
// branch (moss, parent→child); replies_to = water/dialogue (blue);
// supersedes = bark, the old replaced by the new.
const LINK_COLOR: Record<LinkKind, string> = {
  references: 'var(--muted)',
  atom_of: 'var(--moss)',
  replies_to: '#5c89a8',
  supersedes: 'var(--bark)',
}

const MAX_TAG_DOTS = 4

interface PlantNodeData extends Record<string, unknown> {
  note: Note
  tagDots: TagBrief[]
  extraTagCount: number
  dimmed: boolean
  highlighted: boolean
  degree: number
  entropy: number
  onOpen: (id: string) => void
}

function PlantNode({ data }: NodeProps<Node<PlantNodeData>>) {
  const { note, tagDots, extraTagCount, dimmed, highlighted, degree, entropy, onOpen } = data
  const maturity = note.maturity ?? 'seed'
  const glyph = MATURITY_GLYPH[maturity] ?? '🌱'
  const title = (note.title && note.title.trim()) || '·'
  // Border thickness grows with degree (more connected = more
  // developed plant). Log-scale so the high-degree hubs don't dwarf
  // the rest. Cap at 4px so we never blow the layout.
  const borderWidth = Math.min(4, 1.2 + Math.log(1 + degree) * 0.7)
  // Bloom halo: variety of the neighbourhood's generic tags
  // (Shannon entropy 0..1). High variety = the idea sits at a
  // cross-pollination point, so its halo glows wider. Below a
  // small threshold we drop the halo entirely so quiet nodes look
  // quiet.
  const haloRadius = entropy > 0.05 ? 6 + entropy * 18 : 0
  const haloMix = Math.round(25 + entropy * 55)
  const style: CSSProperties = {
    borderWidth,
  }
  if (haloRadius > 0) {
    style.boxShadow = `0 0 ${haloRadius}px color-mix(in srgb, var(--bloom) ${haloMix}%, transparent)`
  }
  return (
    <div
      className={
        'mm-node' +
        (dimmed ? ' mm-node--dimmed' : '') +
        (highlighted ? ' mm-node--highlighted' : '') +
        (note.promoted_at ? ' mm-node--promoted' : '')
      }
      style={style}
      onDoubleClick={() => onOpen(note.id)}
      title={title}
    >
      <Handle
        type="target"
        position={Position.Left}
        className="mm-node__handle mm-node__handle--in"
      />
      <span
        className={`mm-node__glyph mm-node__glyph--${note.maturity}`}
        aria-hidden="true"
      >
        {glyph}
      </span>
      <span className="mm-node__title">{title}</span>
      {(tagDots.length > 0 || extraTagCount > 0) && (
        <span className="mm-node__tags" aria-hidden="true">
          {tagDots.map((tg) => (
            <span
              key={tg.id}
              className="mm-node__tag-dot"
              style={tg.color ? { background: tg.color } : undefined}
              title={tg.name}
            />
          ))}
          {extraTagCount > 0 && (
            <span className="mm-node__tag-more">+{extraTagCount}</span>
          )}
        </span>
      )}
      <Handle
        type="source"
        position={Position.Right}
        className="mm-node__handle mm-node__handle--out"
      />
    </div>
  )
}

const NODE_TYPES = { plant: PlantNode }

function positionsStorageKey(workspaceId: string): string {
  return `flow.garden.mindmap.positions.${workspaceId}`
}

function loadPositions(workspaceId: string): Record<string, { x: number; y: number }> {
  try {
    const raw = localStorage.getItem(positionsStorageKey(workspaceId))
    if (!raw) return {}
    const parsed = JSON.parse(raw) as unknown
    if (parsed && typeof parsed === 'object') {
      return parsed as Record<string, { x: number; y: number }>
    }
  } catch {
    // fall through: corrupt storage, start fresh
  }
  return {}
}

function savePositions(
  workspaceId: string,
  positions: Record<string, { x: number; y: number }>,
): void {
  try {
    localStorage.setItem(positionsStorageKey(workspaceId), JSON.stringify(positions))
  } catch {
    // quota full: skip; positions will recompute on reload
  }
}

// Deterministic seed layout: primary tag groups orbit a center, notes
// without a generic tag form an outer ring. Stable across reloads
// because order is by sorted id, not insertion order. Cleanly replaced
// by user drag positions (persisted), which always win.
function seedLayout(
  notes: Note[],
  primaryTagFor: (n: Note) => TagBrief | null,
): Record<string, { x: number; y: number }> {
  const byTag = new Map<string, Note[]>()
  const untagged: Note[] = []
  for (const n of notes) {
    const tag = primaryTagFor(n)
    if (tag) {
      const arr = byTag.get(tag.id) ?? []
      arr.push(n)
      byTag.set(tag.id, arr)
    } else {
      untagged.push(n)
    }
  }
  const out: Record<string, { x: number; y: number }> = {}
  const groups = Array.from(byTag.entries()).sort(([a], [b]) => a.localeCompare(b))
  const center = { x: 0, y: 0 }
  const groupRadius = 320
  const memberRadius = 110
  groups.forEach(([, members], gi) => {
    const angle = (gi / Math.max(1, groups.length)) * Math.PI * 2
    const cx = center.x + Math.cos(angle) * groupRadius
    const cy = center.y + Math.sin(angle) * groupRadius
    members.sort((a, b) => a.id.localeCompare(b.id))
    members.forEach((m, mi) => {
      const a = (mi / Math.max(1, members.length)) * Math.PI * 2
      out[m.id] = {
        x: cx + Math.cos(a) * memberRadius,
        y: cy + Math.sin(a) * memberRadius,
      }
    })
  })
  untagged.sort((a, b) => a.id.localeCompare(b.id))
  const ringR = groupRadius + memberRadius * 2 + 60
  untagged.forEach((n, i) => {
    const a = (i / Math.max(1, untagged.length)) * Math.PI * 2
    out[n.id] = { x: Math.cos(a) * ringR, y: Math.sin(a) * ringR }
  })
  return out
}

// Tag-derived edges: pairs of notes sharing at least one generic tag.
// Client/project tags are excluded because they're coarse buckets the
// garden already groups by — drawing them as edges produces a hairball
// without information gain. The edge "weight" is the number of shared
// tags (used to bump opacity slightly when the overlap is strong).
function buildTagEdges(
  notes: Note[],
  noteIds: Set<string>,
): { source: string; target: string; sharedTags: TagBrief[] }[] {
  const genericTags = new Map<string, Set<string>>()
  const tagById = new Map<string, TagBrief>()
  for (const n of notes) {
    if (!noteIds.has(n.id)) continue
    for (const t of n.tags ?? []) {
      if (t.kind !== 'generic') continue
      tagById.set(t.id, t)
      const s = genericTags.get(t.id) ?? new Set()
      s.add(n.id)
      genericTags.set(t.id, s)
    }
  }
  const pairToTags = new Map<string, TagBrief[]>()
  for (const [tagId, members] of genericTags) {
    const tag = tagById.get(tagId)
    if (!tag) continue
    const arr = Array.from(members).sort()
    for (let i = 0; i < arr.length; i++) {
      for (let j = i + 1; j < arr.length; j++) {
        const key = `${arr[i]}::${arr[j]}`
        const list = pairToTags.get(key) ?? []
        list.push(tag)
        pairToTags.set(key, list)
      }
    }
  }
  const out: { source: string; target: string; sharedTags: TagBrief[] }[] = []
  for (const [key, sharedTags] of pairToTags) {
    const [source, target] = key.split('::')
    out.push({ source, target, sharedTags })
  }
  return out
}

export interface GardenMindmapProps {
  notes: Note[]
  workspaceId: string
  onOpenNote: (noteId: string) => void
}

export function GardenMindmap(props: GardenMindmapProps) {
  return (
    <ReactFlowProvider>
      <GardenMindmapInner {...props} />
    </ReactFlowProvider>
  )
}

function GardenMindmapInner({ notes, workspaceId, onOpenNote }: GardenMindmapProps) {
  const { t } = useTranslation()
  const [workspaceLinks, setWorkspaceLinks] = useState<WorkspaceLink[]>([])
  const [showTagEdges, setShowTagEdges] = useState(true)
  const [search, setSearch] = useState('')
  const [pendingConnect, setPendingConnect] = useState<Connection | null>(null)
  const [linkKind, setLinkKind] = useState<LinkKind>('references')
  const [err, setErr] = useState('')
  const positionsRef = useRef<Record<string, { x: number; y: number }>>({})
  const rf = useReactFlow()

  const primaryTagFor = useCallback((n: Note): TagBrief | null => {
    const generic = (n.tags ?? []).find((t) => t.kind === 'generic')
    if (generic) return generic
    const project = (n.tags ?? []).find((t) => t.kind === 'project')
    return project ?? null
  }, [])

  // Stable note set (id-keyed) for reactivity of derived structures.
  const noteIds = useMemo(() => new Set(notes.map((n) => n.id)), [notes])
  const noteById = useMemo(() => {
    const m = new Map<string, Note>()
    for (const n of notes) m.set(n.id, n)
    return m
  }, [notes])

  const searchMatch = useCallback(
    (n: Note): boolean => {
      const q = search.trim().toLowerCase()
      if (!q) return false
      const hay = ((n.title || '') + ' ' + (n.transcript || '')).toLowerCase()
      return hay.includes(q)
    },
    [search],
  )

  // Degree of each visible node: counts typed manual links between
  // notes in scope. Tag-derived edges are excluded because they're a
  // visual aid, not an authored relationship — we don't want a node
  // to appear "developed" just because it shares one generic tag
  // with many neighbours.
  const degreeByNode = useMemo(() => {
    const map = new Map<string, number>()
    for (const l of workspaceLinks) {
      if (!noteIds.has(l.parent_note_id) || !noteIds.has(l.child_note_id)) continue
      map.set(l.parent_note_id, (map.get(l.parent_note_id) ?? 0) + 1)
      map.set(l.child_note_id, (map.get(l.child_note_id) ?? 0) + 1)
    }
    return map
  }, [workspaceLinks, noteIds])

  // Tag entropy of each node's neighbourhood: 0 = monoculture
  // (every neighbour shares the same generic tags), 1 = max variety.
  // Shannon entropy normalised by log2(distinct-tag-count) so the
  // value stays comparable across nodes with different fan-out.
  // Drives the bloom halo intensity in PlantNode: high-entropy
  // nodes are cross-pollination points and bloom wider.
  const entropyByNode = useMemo(() => {
    const adj = new Map<string, Set<string>>()
    for (const l of workspaceLinks) {
      if (!noteIds.has(l.parent_note_id) || !noteIds.has(l.child_note_id)) continue
      const a = adj.get(l.parent_note_id) ?? new Set<string>()
      a.add(l.child_note_id)
      adj.set(l.parent_note_id, a)
      const b = adj.get(l.child_note_id) ?? new Set<string>()
      b.add(l.parent_note_id)
      adj.set(l.child_note_id, b)
    }
    const result = new Map<string, number>()
    for (const n of notes) {
      const neighbours = adj.get(n.id)
      if (!neighbours || neighbours.size === 0) {
        result.set(n.id, 0)
        continue
      }
      const counts = new Map<string, number>()
      let total = 0
      for (const nbId of neighbours) {
        const nb = noteById.get(nbId)
        if (!nb) continue
        for (const tg of nb.tags ?? []) {
          if (tg.kind !== 'generic') continue
          counts.set(tg.id, (counts.get(tg.id) ?? 0) + 1)
          total += 1
        }
      }
      if (total === 0 || counts.size <= 1) {
        result.set(n.id, 0)
        continue
      }
      let H = 0
      for (const c of counts.values()) {
        const p = c / total
        H -= p * Math.log2(p)
      }
      const Hmax = Math.log2(counts.size)
      result.set(n.id, Hmax > 0 ? Math.min(1, H / Hmax) : 0)
    }
    return result
  }, [workspaceLinks, noteIds, noteById, notes])

  // Tag dots shown inside the node (forest "blooms"). Exclude the
  // memory_channel kind (system bookkeeping, never user-meaningful)
  // and cap at MAX_TAG_DOTS — overflow is summarised as a +N pill.
  const tagDotsFor = useCallback((n: Note): { dots: TagBrief[]; extra: number } => {
    const visible = (n.tags ?? []).filter((t) => t.kind !== 'memory_channel')
    const dots = visible.slice(0, MAX_TAG_DOTS)
    const extra = Math.max(0, visible.length - dots.length)
    return { dots, extra }
  }, [])

  // Initial nodes — built once per notes-set; user drag positions
  // are merged from localStorage. We compute outside the useNodesState
  // initializer so that subsequent notes changes (load completes,
  // tag change, focus change) can resync via a setter effect below.
  const initialNodes = useMemo<Node<PlantNodeData>[]>(() => {
    const stored = loadPositions(workspaceId)
    const seeded = seedLayout(notes, primaryTagFor)
    return notes.map((n) => {
      const pos = stored[n.id] ?? seeded[n.id] ?? { x: 0, y: 0 }
      const dimmed = n.maturity === 'dormant' || Boolean(n.promoted_at)
      const { dots, extra } = tagDotsFor(n)
      return {
        id: n.id,
        type: 'plant',
        position: pos,
        data: {
          note: n,
          tagDots: dots,
          extraTagCount: extra,
          dimmed,
          highlighted: false,
          degree: degreeByNode.get(n.id) ?? 0,
          entropy: entropyByNode.get(n.id) ?? 0,
          onOpen: onOpenNote,
        },
        draggable: true,
      }
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspaceId])

  const [nodes, setNodes, onNodesChange] = useNodesState<Node<PlantNodeData>>(
    initialNodes,
  )
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([])

  // Sync nodes when the notes set changes (focus filter toggled,
  // new note created, etc.). Preserves any in-memory user drag
  // positions for notes that survive across the change.
  useEffect(() => {
    const stored = loadPositions(workspaceId)
    const seeded = seedLayout(notes, primaryTagFor)
    setNodes((current) => {
      const byId = new Map(current.map((c) => [c.id, c]))
      return notes.map((n) => {
        const existing = byId.get(n.id)
        const pos =
          existing?.position ?? stored[n.id] ?? seeded[n.id] ?? { x: 0, y: 0 }
        const dimmed = n.maturity === 'dormant' || Boolean(n.promoted_at)
        const { dots, extra } = tagDotsFor(n)
        return {
          id: n.id,
          type: 'plant',
          position: pos,
          data: {
            note: n,
            tagDots: dots,
            extraTagCount: extra,
            dimmed,
            highlighted: search ? searchMatch(n) : false,
            degree: degreeByNode.get(n.id) ?? 0,
            entropy: entropyByNode.get(n.id) ?? 0,
            onOpen: onOpenNote,
          },
          draggable: true,
        }
      })
    })
  }, [
    notes,
    workspaceId,
    primaryTagFor,
    search,
    searchMatch,
    onOpenNote,
    setNodes,
    degreeByNode,
    entropyByNode,
    tagDotsFor,
  ])

  // Persist position changes (debounced via rAF microtask is overkill
  // for human-paced drag; we save on each NodeChange of kind 'position'
  // whose dragging flag has just flipped to false).
  useEffect(() => {
    const positions: Record<string, { x: number; y: number }> = {}
    for (const n of nodes) {
      positions[n.id] = { x: n.position.x, y: n.position.y }
    }
    positionsRef.current = positions
    savePositions(workspaceId, positions)
  }, [nodes, workspaceId])

  // ---------------------------------------------------------------
  // Workspace links: fetched once on mount, refreshed after every
  // mutation. Not cached cross-component (the garden tab is the only
  // consumer).
  // ---------------------------------------------------------------
  const reloadLinks = useCallback(async () => {
    const res = await authFetch('/notes/links')
    if (!res.ok) {
      setErr(`HTTP ${res.status}`)
      return
    }
    const data = (await res.json()) as WorkspaceLink[]
    setWorkspaceLinks(data)
  }, [])

  useEffect(() => {
    let active = true
    void (async () => {
      const res = await authFetch('/notes/links')
      if (!active) return
      if (!res.ok) {
        setErr(`HTTP ${res.status}`)
        return
      }
      const data = (await res.json()) as WorkspaceLink[]
      if (active) setWorkspaceLinks(data)
    })()
    return () => {
      active = false
    }
  }, [])

  // Compute edges: manual links plus, conditionally, tag-derived ones.
  // Manual edges only between visible notes (filtered scope); tag
  // edges always limited to noteIds for the same reason.
  useEffect(() => {
    const manual: Edge[] = workspaceLinks
      .filter(
        (l) => noteIds.has(l.parent_note_id) && noteIds.has(l.child_note_id),
      )
      .map((l) => {
        const kind = l.kind as LinkKind
        const color = LINK_COLOR[kind] ?? 'var(--moss)'
        // Per-kind stroke vocabulary:
        //  atom_of    → thick solid branch (parent feeds child)
        //  references → thin dashed scent (a passing mention)
        //  replies_to → medium dashed-rhythm (call-and-response)
        //  supersedes → solid + arrowhead (old replaced by new)
        const style: CSSProperties =
          kind === 'atom_of'
            ? { stroke: color, strokeWidth: 2.2 }
            : kind === 'references'
              ? { stroke: color, strokeWidth: 1.2, strokeDasharray: '3 3', opacity: 0.75 }
              : kind === 'replies_to'
                ? { stroke: color, strokeWidth: 1.8, strokeDasharray: '6 2' }
                : { stroke: color, strokeWidth: 2.0, opacity: 0.9 }
        return {
          id: `mm-link-${l.id}`,
          source: l.parent_note_id,
          target: l.child_note_id,
          type: 'default',
          animated: false,
          label: t(`garden.mindmap.linkKind.${l.kind}`),
          labelStyle: { fontSize: 10, fill: 'var(--text)' },
          labelBgStyle: { fill: 'var(--surface)', fillOpacity: 0.85 },
          style,
          markerEnd:
            kind === 'supersedes'
              ? { type: MarkerType.ArrowClosed, color, width: 14, height: 14 }
              : undefined,
          data: { kind: l.kind, linkId: l.id, isManual: true },
        }
      })
    const tagEdges: Edge[] = []
    if (showTagEdges) {
      const computed = buildTagEdges(notes, noteIds)
      for (const e of computed) {
        const w = Math.min(0.6, 0.25 + 0.1 * e.sharedTags.length)
        tagEdges.push({
          id: `mm-tag-${e.source}-${e.target}`,
          source: e.source,
          target: e.target,
          type: 'straight',
          label: e.sharedTags.map((tg) => tg.name).join(' · '),
          labelStyle: { fontSize: 9, fill: 'var(--muted)' },
          labelBgStyle: { fill: 'var(--surface)', fillOpacity: 0.7 },
          style: {
            stroke: e.sharedTags[0]?.color || 'var(--muted)',
            strokeWidth: 1,
            strokeDasharray: '4 3',
            opacity: w,
          },
          data: { isManual: false },
          selectable: false,
        })
      }
    }
    setEdges([...tagEdges, ...manual])
  }, [workspaceLinks, notes, noteIds, showTagEdges, t, setEdges])

  // ---------------------------------------------------------------
  // Edge creation: drag from a node handle to another node opens a
  // kind-selector popover; confirm POSTs the link and refreshes.
  // The optimistic-add into rf state is skipped so we never end up
  // with a "ghost" edge that disagrees with the server.
  // ---------------------------------------------------------------
  const onConnect = useCallback(
    (conn: Connection) => {
      if (!conn.source || !conn.target || conn.source === conn.target) return
      setPendingConnect(conn)
      setLinkKind('references')
    },
    [],
  )

  const confirmCreateLink = useCallback(async () => {
    if (!pendingConnect || !pendingConnect.source || !pendingConnect.target) return
    setErr('')
    const res = await authFetch(`/notes/${pendingConnect.source}/links`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        parent_note_id: pendingConnect.source,
        child_note_id: pendingConnect.target,
        kind: linkKind,
      }),
    })
    if (!res.ok) {
      try {
        const body = (await res.json()) as { detail?: string }
        setErr(body.detail || `HTTP ${res.status}`)
      } catch {
        setErr(`HTTP ${res.status}`)
      }
      return
    }
    setPendingConnect(null)
    await reloadLinks()
  }, [pendingConnect, linkKind, reloadLinks])

  // ---------------------------------------------------------------
  // Edge click: only manual links are interactive. Click → confirm
  // → DELETE. Tag-derived edges are non-selectable, so the handler
  // is only called for solid edges.
  // ---------------------------------------------------------------
  const onEdgeClick = useCallback<EdgeMouseHandler>(
    (event, edge) => {
      event.stopPropagation()
      const data = edge.data as
        | { isManual?: boolean; linkId?: string; kind?: string }
        | undefined
      if (!data?.isManual || !edge.source) return
      if (!window.confirm(t('garden.mindmap.confirmDelete'))) return
      void (async () => {
        const params = new URLSearchParams({
          child_note_id: edge.target,
          kind: data.kind || 'references',
        })
        const res = await authFetch(
          `/notes/${edge.source}/links?${params.toString()}`,
          { method: 'DELETE' },
        )
        if (!res.ok && res.status !== 204) {
          setErr(`HTTP ${res.status}`)
          return
        }
        await reloadLinks()
      })()
    },
    [reloadLinks, t],
  )

  const onNodeClick = useCallback<NodeMouseHandler<Node<PlantNodeData>>>(
    (_event, node) => {
      // Single click does nothing destructive — opening the plant
      // modal is on double-click (PlantNode handles it directly). A
      // single click could still highlight, so we center on the node
      // to mimic the search-hit behavior.
      rf.setCenter(node.position.x, node.position.y, { duration: 200, zoom: 1.1 })
    },
    [rf],
  )

  // Search: filter / highlight + center on the first match.
  useEffect(() => {
    if (!search.trim()) return
    const first = notes.find((n) => searchMatch(n))
    if (first) {
      const pos = positionsRef.current[first.id]
      if (pos) rf.setCenter(pos.x, pos.y, { duration: 250, zoom: 1.2 })
    }
  }, [search, notes, searchMatch, rf])

  const isEmpty = notes.length === 0

  return (
    <div className="garden__mindmap">
      <div className="garden__mindmap-toolbar">
        <input
          type="search"
          className="garden__mindmap-search"
          placeholder={t('garden.mindmap.search')}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <label className="garden__mindmap-toggle">
          <input
            type="checkbox"
            checked={showTagEdges}
            onChange={(e) => setShowTagEdges(e.target.checked)}
          />
          <span>{t('garden.mindmap.tagEdges')}</span>
        </label>
        <span className="garden__mindmap-legend" aria-hidden="true">
          {LINK_KINDS.map((k) => (
            <span key={k} className="garden__mindmap-legend-item">
              <span
                className="garden__mindmap-legend-swatch"
                style={{ background: LINK_COLOR[k] }}
              />
              {t(`garden.mindmap.linkKind.${k}`)}
            </span>
          ))}
        </span>
      </div>

      {err && <p className="err">{err}</p>}

      {isEmpty ? (
        <p className="hint garden__empty">{t('garden.mindmap.empty')}</p>
      ) : (
        <div className="garden__mindmap-canvas">
          <ReactFlow
            nodes={nodes}
            edges={edges}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onConnect={onConnect}
            onEdgeClick={onEdgeClick}
            onNodeClick={onNodeClick}
            nodeTypes={NODE_TYPES}
            fitView
            fitViewOptions={{ padding: 0.25, maxZoom: 1.4 }}
            proOptions={{ hideAttribution: true }}
            minZoom={0.2}
            maxZoom={2.5}
            nodesConnectable
            elementsSelectable
          >
            <Background gap={28} size={1} />
            <Controls showInteractive={false} />
            <MiniMap
              pannable
              zoomable
              nodeColor={(n) => {
                const data = n.data as PlantNodeData | undefined
                if (data?.dimmed) return 'var(--muted)'
                const first = data?.tagDots?.[0]
                return first?.color || 'var(--moss)'
              }}
              maskColor="rgba(0,0,0,0.18)"
            />
          </ReactFlow>
        </div>
      )}

      {pendingConnect && (
        <div
          className="modal__backdrop"
          role="dialog"
          aria-modal="true"
          onClick={(e) => {
            if (e.target === e.currentTarget) setPendingConnect(null)
          }}
        >
          <div className="modal__panel modal__panel--narrow">
            <div className="modal__head">
              <strong>{t('garden.mindmap.createLink')}</strong>
              <span className="modal__sp" />
              <button
                type="button"
                className="btn--ghost btn--sm"
                onClick={() => setPendingConnect(null)}
              >
                {t('notes.close')}
              </button>
            </div>
            <div className="modal__body">
              <p className="hint">
                <strong>
                  {noteById.get(pendingConnect.source ?? '')?.title || '·'}
                </strong>
                {' → '}
                <strong>
                  {noteById.get(pendingConnect.target ?? '')?.title || '·'}
                </strong>
              </p>
              <div className="garden__mindmap-kindlist" role="radiogroup">
                {LINK_KINDS.map((k) => (
                  <label key={k} className="garden__mindmap-kindopt">
                    <input
                      type="radio"
                      name="link-kind"
                      value={k}
                      checked={linkKind === k}
                      onChange={() => setLinkKind(k)}
                    />
                    <span
                      className="garden__mindmap-legend-swatch"
                      style={{ background: LINK_COLOR[k] }}
                    />
                    <span>{t(`garden.mindmap.linkKind.${k}`)}</span>
                  </label>
                ))}
              </div>
              <div className="modal__foot">
                <button
                  type="button"
                  className="btn"
                  onClick={() => void confirmCreateLink()}
                >
                  {t('garden.mindmap.createLink')}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
