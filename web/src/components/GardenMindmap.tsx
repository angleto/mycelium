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
  type NodeChange,
  type NodeMouseHandler,
  type NodeProps,
} from '@xyflow/react'
import '@xyflow/react/dist/base.css'
import { useTranslation } from 'react-i18next'

import { authFetch } from '../api/client'
import type { components } from '../api/schema'

type Note = components['schemas']['NoteOut']
type TagBrief = components['schemas']['TagBrief']

// The mycelial 4-verb model (ADR-0040). ``related`` is UNDIRECTED (the
// server canonicalises its endpoints); the others are directional. All
// four are user-creatable. ``supersedes`` / ``contradicts`` also decay
// the target toward dormant server-side.
type LinkKind = 'hypha_of' | 'related' | 'supersedes' | 'contradicts'
const LINK_KINDS: LinkKind[] = ['hypha_of', 'related', 'supersedes', 'contradicts']
// Undirected kinds: the two connection handles are interchangeable and
// the create dialog shows no direction control.
const UNDIRECTED_KINDS: ReadonlySet<LinkKind> = new Set<LinkKind>(['related'])

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
// hypha_of = a living filament grown from another idea (moss, directional);
// related = a neutral connection in the weave (muted, undirected);
// supersedes = bark, the old replaced by the new; contradicts = a struck
// filament that kills the target (rust-red).
const LINK_COLOR: Record<LinkKind, string> = {
  hypha_of: 'var(--moss)',
  related: 'var(--muted)',
  supersedes: 'var(--bark)',
  contradicts: '#b0553f',
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
  centrality: number | null
  walkStep: number | null
  isWalkSeed: boolean
  // Focus+context overlay (render-only): set by the displayNodes
  // memo when a node is hovered/pinned. ``focus`` = the node itself,
  // ``neighbor`` = directly linked, ``faded`` = out of focus.
  focusState?: 'focus' | 'neighbor' | 'faded' | null
  onOpen: (id: string) => void
}

function PlantNode({ data }: NodeProps<Node<PlantNodeData>>) {
  const {
    note,
    tagDots,
    extraTagCount,
    dimmed,
    highlighted,
    degree,
    entropy,
    centrality,
    walkStep,
    isWalkSeed,
    focusState,
    onOpen,
  } = data
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
  // Only genuinely cross-pollinating nodes (high neighbourhood tag
  // entropy) bloom, and the radius is capped tighter than before: an
  // always-on glow on every connected node merged into fog in dense
  // clusters and read as extra crowding.
  const haloRadius = entropy > 0.35 ? 4 + entropy * 10 : 0
  const haloMix = Math.round(20 + entropy * 35)
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
        (note.promoted_at ? ' mm-node--promoted' : '') +
        (focusState === 'faded' ? ' mm-node--faded' : '') +
        (focusState === 'neighbor' ? ' mm-node--neighbor' : '') +
        (focusState === 'focus' ? ' mm-node--focus' : '') +
        (walkStep != null ? ' mm-node--walk' : '') +
        (isWalkSeed ? ' mm-node--walk-seed' : '')
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
      {centrality != null && (
        <span
          className="mm-node__centrality"
          title={`PageRank ${centrality.toFixed(4)}`}
        >
          {centrality < 0.0005 ? '<0.001' : centrality.toFixed(3)}
        </span>
      )}
      {walkStep != null && (
        <span className="mm-node__walk-step" title={`walk step ${walkStep}`}>
          {walkStep}
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

// ``v2`` because v1 persisted on every nodes-state change, which
// burned the first-render seedLayout pile-up into localStorage and
// then loaded it back over the force layout forever. v2 only writes
// on a real drag-end, so this key bump is also a clean-slate for
// every workspace that suffered the v1 behaviour.
function positionsStorageKey(workspaceId: string): string {
  return `flow.garden.mindmap.positions.v2.${workspaceId}`
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

// Soft-OR saturating combine of two [0,1] weights (task 7e99c724).
// Two evidence sources for the same fact don't add linearly (they'd
// blow past 1); they accumulate with diminishing returns.
function softOr(a: number, b: number): number {
  return 1 - (1 - a) * (1 - b)
}

// Per-kind base weight contribution: how much each link type pulls
// two nodes together. Mirrors the server's _KIND_WEIGHT so the rendered
// thickness matches the materialised edge strength (hypha_of = the
// strongest filament, related = a light neutral thread).
const KIND_WEIGHT: Record<string, number> = {
  hypha_of: 0.85,
  supersedes: 0.7,
  contradicts: 0.65,
  related: 0.45,
}

// Edge weight v1 = soft-OR of the kind base and a tag-overlap term
// (more shared generic tags = stronger context). Tag overlap is
// derived from the visible note set so it stays consistent with the
// tag-edges layer. v2 (task 4467acb4) will read this from the
// note_edge_strength materialised view.
function edgeWeightV1(
  kind: string,
  sharedGenericTags: number,
): number {
  const wKind = KIND_WEIGHT[kind] ?? 0.4
  const wTag = 1 - 1 / (1 + 0.4 * sharedGenericTags)
  return softOr(wKind, wTag)
}

// Count shared generic tags between two notes. Cheap O(|tags_a|).
function sharedGenericTagCount(a: Note | undefined, b: Note | undefined): number {
  if (!a || !b) return 0
  const aIds = new Set(
    (a.tags ?? []).filter((t) => t.kind === 'generic').map((t) => t.id),
  )
  if (aIds.size === 0) return 0
  let n = 0
  for (const t of b.tags ?? []) {
    if (t.kind === 'generic' && aIds.has(t.id)) n++
  }
  return n
}

// Mini force simulator (task 7e99c724 v1): repulsion + spring along
// edges + centripetal gravity proportional to centrality.
// Deterministic seed (sorted notes by id, no randomness), bounded
// budget (250 ticks), zero deps. The result is a final positions
// map the caller merges with stored user-drag positions (which still
// win). Sub-100ms on a few hundred nodes.
function forceLayout(
  notes: Note[],
  links: { source: string; target: string; weight: number }[],
  opts: {
    centrality: Map<string, number>
    clusterOf: (id: string) => string
  },
): Record<string, { x: number; y: number }> {
  const N = notes.length
  if (N === 0) return {}
  // Centrality (PageRank from /garden/graph) normalised to [0,1]. The
  // hub of each cluster is its highest-centrality node and is pulled
  // hardest toward the cluster centroid, so it sits visually central
  // while leaves splay outward into rings ("roots at the centre,
  // leaves at the rim").
  let maxC = 0
  for (const n of notes) maxC = Math.max(maxC, opts.centrality.get(n.id) ?? 0)
  const cNorm = (id: string): number =>
    maxC > 0 ? (opts.centrality.get(id) ?? 0) / maxC : 0
  const phi = Math.PI * (3 - Math.sqrt(5))
  const sorted = [...notes].sort((a, b) => a.id.localeCompare(b.id))
  const idx = new Map<string, number>(sorted.map((n, i) => [n.id, i]))
  const x = new Float64Array(N)
  const y = new Float64Array(N)
  // Per-node cluster centroid + gravity weight (filled per glade).
  const cgx = new Float64Array(N)
  const cgy = new Float64Array(N)
  const gravW = new Float64Array(N)
  // Group notes into clusters ("glades"): Leiden community when
  // available, else the primary-tag bucket, else one shared glade.
  // Each glade gets a centroid on a golden spiral, spaced by its size
  // so big communities claim more room and the glades stay clear.
  const groups = new Map<string, number[]>()
  for (let i = 0; i < N; i++) {
    const key = opts.clusterOf(sorted[i].id)
    const arr = groups.get(key) ?? []
    arr.push(i)
    groups.set(key, arr)
  }
  const clusterKeys = [...groups.keys()].sort((a, b) => {
    const sa = groups.get(a)?.length ?? 0
    const sb = groups.get(b)?.length ?? 0
    return sb - sa || a.localeCompare(b)
  })
  clusterKeys.forEach((key, ci) => {
    const members = groups.get(key) ?? []
    const size = members.length
    const rIntra = 110 + 44 * Math.sqrt(size)
    const ca = ci * phi
    const rCent = (rIntra + 240) * Math.sqrt(ci + 0.5)
    const ccx = Math.cos(ca) * rCent
    const ccy = Math.sin(ca) * rCent
    // Highest centrality at the centroid; the rest on rings by
    // centrality rank with a golden-angle angular slot.
    const ordered = [...members].sort((ia, ib) => {
      const da = cNorm(sorted[ia].id)
      const db = cNorm(sorted[ib].id)
      return db - da || sorted[ia].id.localeCompare(sorted[ib].id)
    })
    const ringUnit = rIntra / Math.max(1, Math.sqrt(size))
    ordered.forEach((i, rank) => {
      cgx[i] = ccx
      cgy[i] = ccy
      // Hubs cling to the centroid; leaves drift out.
      gravW[i] = 0.4 + 1.6 * cNorm(sorted[i].id)
      const rr = Math.sqrt(rank) * ringUnit
      const aa = rank * phi
      x[i] = ccx + Math.cos(aa) * rr
      y[i] = ccy + Math.sin(aa) * rr
    })
  })
  // Edge list with weighted spring rest length: heavy edge = short
  // rest length (nodes pulled closer). Skip edges with an endpoint
  // outside the visible set.
  const edges: { a: number; b: number; rest: number; k: number }[] = []
  for (const l of links) {
    const ia = idx.get(l.source)
    const ib = idx.get(l.target)
    if (ia === undefined || ib === undefined) continue
    const w = Math.max(0, Math.min(1, l.weight))
    // L0 = 150 / (0.35 + 0.65w): w=1 -> ~150 (one chip width, so even
    // a heavy edge settles with the two chips clear of each other),
    // w=0 -> ~430. Was 80/(0.3+0.7w) which let linked chips overlap.
    edges.push({ a: ia, b: ib, rest: 150 / (0.35 + 0.65 * w), k: 0.06 + 0.12 * w })
  }
  const REPULSION_K = 2600
  const GRAVITY_BETA = 0.018
  const DAMPING = 0.82
  const MAX_STEP = 18
  const TICKS = 250
  const dx = new Float64Array(N)
  const dy = new Float64Array(N)
  let alpha = 1
  for (let tick = 0; tick < TICKS; tick++) {
    dx.fill(0)
    dy.fill(0)
    // Repulsion: O(N²). For N<300 (~typical garden) this is fine.
    for (let i = 0; i < N; i++) {
      for (let j = i + 1; j < N; j++) {
        let rx = x[i] - x[j]
        let ry = y[i] - y[j]
        let d2 = rx * rx + ry * ry
        if (d2 < 1) {
          rx = (i - j) * 0.5
          ry = (j - i) * 0.5
          d2 = 1
        }
        const f = REPULSION_K / d2
        const inv = 1 / Math.sqrt(d2)
        dx[i] += f * rx * inv
        dy[i] += f * ry * inv
        dx[j] -= f * rx * inv
        dy[j] -= f * ry * inv
      }
    }
    // Spring attraction along edges
    for (const e of edges) {
      const rx = x[e.b] - x[e.a]
      const ry = y[e.b] - y[e.a]
      const d = Math.max(1, Math.sqrt(rx * rx + ry * ry))
      const f = e.k * (d - e.rest)
      const fx = (rx / d) * f
      const fy = (ry / d) * f
      dx[e.a] += fx
      dy[e.a] += fy
      dx[e.b] -= fx
      dy[e.b] -= fy
    }
    // Centripetal gravity toward each node's CLUSTER centroid (not the
    // global origin), scaled by centrality: hubs are pulled hard to the
    // glade centre, leaves only lightly, so each glade reads as a hub
    // ringed by its leaves. Replaces the old degree-to-origin pull that
    // produced an undifferentiated blob.
    for (let i = 0; i < N; i++) {
      dx[i] -= GRAVITY_BETA * gravW[i] * (x[i] - cgx[i])
      dy[i] -= GRAVITY_BETA * gravW[i] * (y[i] - cgy[i])
    }
    // Integrate with cooling alpha + per-tick step clamp so a single
    // huge repulsion impulse can't catapult a node off-canvas.
    for (let i = 0; i < N; i++) {
      let sx = dx[i] * alpha * DAMPING
      let sy = dy[i] * alpha * DAMPING
      const mag = Math.sqrt(sx * sx + sy * sy)
      if (mag > MAX_STEP) {
        sx = (sx / mag) * MAX_STEP
        sy = (sy / mag) * MAX_STEP
      }
      x[i] += sx
      y[i] += sy
    }
    alpha *= 0.985
    if (alpha < 0.05) break
  }
  // Overlap relaxation. The force field above treats every node as a
  // point, but a chip is a wide-short box, so its equilibrium spacing
  // is happily smaller than the node itself and chips end up sitting
  // on top of each other -- the literal "crowded chips". Treat each
  // node as an axis-aligned box (half-extents incl. margin) and push
  // overlapping pairs apart along their axis of least penetration.
  // Fully deterministic (id-sorted order + a fixed i<j tie-break),
  // one-shot, O(N^2 * passes) which is sub-ms at a few hundred nodes.
  // forceLayout's output is still overridden by stored drag positions
  // at the call site, so user placement keeps winning.
  const HALF_W = 96
  const HALF_H = 28
  for (let pass = 0; pass < 16; pass++) {
    let moved = false
    for (let i = 0; i < N; i++) {
      for (let j = i + 1; j < N; j++) {
        const rx = x[i] - x[j]
        const ry = y[i] - y[j]
        const ox = 2 * HALF_W - Math.abs(rx)
        const oy = 2 * HALF_H - Math.abs(ry)
        if (ox <= 0 || oy <= 0) continue
        if (ox < oy) {
          const push = ox / 2
          const s = rx === 0 ? 1 : Math.sign(rx)
          x[i] += s * push
          x[j] -= s * push
        } else {
          const push = oy / 2
          const s = ry === 0 ? 1 : Math.sign(ry)
          y[i] += s * push
          y[j] -= s * push
        }
        moved = true
      }
    }
    if (!moved) break
  }
  const out: Record<string, { x: number; y: number }> = {}
  for (let i = 0; i < N; i++) out[sorted[i].id] = { x: x[i], y: y[i] }
  return out
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
  // Off by default: the all-pairs shared-tag layer is the bulk of the
  // hairball on first paint. The toggle keeps it one click away, and
  // the layout still clusters by tag via the tag-springs above, so
  // turning the visual layer off doesn't move any nodes.
  const [showTagEdges, setShowTagEdges] = useState(false)
  const [showCentrality, setShowCentrality] = useState(false)
  const [showEdgeWeights, setShowEdgeWeights] = useState(false)
  const [centrality, setCentrality] = useState<Record<string, number>>({})
  const [edgeWeightMap, setEdgeWeightMap] = useState<Record<string, number>>({})
  // Leiden communities from /garden/clusters (task 8c0a8f08). Empty when
  // the optional clustering extra (igraph + leidenalg) is not installed;
  // the layout then falls back to primary-tag glades.
  const [clusters, setClusters] = useState<Record<string, number>>({})
  // Walk (task 5bf31b63): seed + mode + path. The user selects a
  // node, clicks 'walk', and the path lights up as the pollinator
  // trail. ``walkPath`` keys the step index per node id so the
  // PlantNode renderer can render a halo + step badge. ``walkSeq`` is
  // the ordered list of visited node ids (free_wander includes
  // revisits) and ``walkResultMode`` records the mode that produced the
  // current walk: together they drive the illuminated trail overlaid on
  // the edges. Focused walks are a PPR-ranked set, not a path, so they
  // light up nodes only — there is no trajectory to draw.
  const [walkMode, setWalkMode] = useState<'focused' | 'free_wander'>('focused')
  const [walkSeed, setWalkSeed] = useState<string | null>(null)
  const [walkPath, setWalkPath] = useState<Record<string, number>>({})
  const [walkSeq, setWalkSeq] = useState<string[]>([])
  const [walkResultMode, setWalkResultMode] = useState<
    'focused' | 'free_wander'
  >('focused')
  const [search, setSearch] = useState('')
  const [pendingConnect, setPendingConnect] = useState<Connection | null>(null)
  const [linkKind, setLinkKind] = useState<LinkKind>('related')
  const [err, setErr] = useState('')
  // Focus+context: hovering a node sets a transient focus; clicking
  // pins it (toggle) so it survives the mouse leaving. The effective
  // focus (activeFocus) is derived further down, once noteIds exists,
  // by validating the pin/hover against the live note set. ``lod`` is
  // a coarse zoom bucket ('far' collapses chips to their glyph),
  // flipped by onMove only on a threshold crossing. ``isDragging``
  // gates the focus overlay so a hover during a drag doesn't re-map
  // every node on each pointer tick.
  const [hoverId, setHoverId] = useState<string | null>(null)
  const [pinnedId, setPinnedId] = useState<string | null>(null)
  const [lod, setLod] = useState<'near' | 'far'>('near')
  const [isDragging, setIsDragging] = useState(false)
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

  // Centrality as a Map for the layout (PageRank from /garden/graph).
  const centralityMap = useMemo(
    () => new Map(Object.entries(centrality)),
    [centrality],
  )
  // Cluster key per note for the layout: Leiden community when present,
  // else the primary-tag bucket, else a single shared glade. This is
  // the grouping the cluster-radial layout lays out as separate glades.
  const clusterKeyFor = useCallback(
    (id: string): string => {
      const c = clusters[id]
      if (c != null) return `c${c}`
      const note = noteById.get(id)
      const tag = note ? primaryTagFor(note) : null
      return tag ? `t${tag.id}` : 'none'
    },
    [clusters, noteById, primaryTagFor],
  )

  // Effective focus, derived (not stored) so it self-heals: a pin or
  // hover whose note has left the visible set (focus filter, delete)
  // is ignored rather than greying out the whole canvas, and hover
  // can take over immediately without the user clearing a dead pin.
  const activeFocus =
    (pinnedId && noteIds.has(pinnedId) ? pinnedId : null) ??
    (hoverId && noteIds.has(hoverId) ? hoverId : null)

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

  // Force-layout input: weighted edges between visible notes.
  // Manual links carry their kind-derived weight (soft-OR of kind +
  // shared-tag count); tag-derived edges add a light spring so the
  // simulator still has something to organise around in workspaces
  // that haven't drawn a single manual link yet -- without them
  // seedLayout's tag-clustered ring is the only signal and notes
  // sharing a single client tag collapse into one pile. Tag-spring
  // weight is capped (0.45) so a manual link always pulls harder.
  const weightedLinks = useMemo(() => {
    const out: { source: string; target: string; weight: number }[] = []
    const seen = new Set<string>()
    const pairKey = (a: string, b: string) => (a < b ? `${a}::${b}` : `${b}::${a}`)
    for (const l of workspaceLinks) {
      if (!noteIds.has(l.parent_note_id) || !noteIds.has(l.child_note_id)) continue
      const shared = sharedGenericTagCount(
        noteById.get(l.parent_note_id),
        noteById.get(l.child_note_id),
      )
      out.push({
        source: l.parent_note_id,
        target: l.child_note_id,
        weight: edgeWeightV1(l.kind, shared),
      })
      seen.add(pairKey(l.parent_note_id, l.child_note_id))
    }
    // Tag-springs give the simulator something to cluster around in
    // workspaces with few manual links. A naive all-pairs spring per
    // shared tag is O(k^2) and collapses a 20-note tag into one tight
    // pile regardless of repulsion, so instead anchor each tagged
    // note to its tag group's lowest-id member: a deterministic star
    // (k-1 springs, not k(k-1)/2). Spatial clustering is preserved,
    // the quadratic crush is gone. The VISUAL tag-edge layer (in the
    // edges effect) is independent and unaffected by this.
    const genericGroups = new Map<string, string[]>()
    for (const n of notes) {
      if (!noteIds.has(n.id)) continue
      for (const tg of n.tags ?? []) {
        if (tg.kind !== 'generic') continue
        const arr = genericGroups.get(tg.id) ?? []
        arr.push(n.id)
        genericGroups.set(tg.id, arr)
      }
    }
    for (const members of genericGroups.values()) {
      if (members.length < 2) continue
      const sortedMembers = [...members].sort((a, b) => a.localeCompare(b))
      const anchor = sortedMembers[0]
      for (let i = 1; i < sortedMembers.length; i++) {
        const key = pairKey(anchor, sortedMembers[i])
        if (seen.has(key)) continue
        // Light, flat weight: enough to organise, light enough that an
        // authored link always pulls harder.
        out.push({ source: anchor, target: sortedMembers[i], weight: 0.35 })
        seen.add(key)
      }
    }
    return out
  }, [workspaceLinks, noteIds, noteById, notes])

  // Initial nodes — built once per notes-set; user drag positions
  // are merged from localStorage. We compute outside the useNodesState
  // initializer so that subsequent notes changes (load completes,
  // tag change, focus change) can resync via a setter effect below.
  // Layout strategy: force-directed when the visible subgraph has
  // any manual links (the simulator gives an organic, centrality-
  // driven shape); fall back to the deterministic seedLayout when
  // there's nothing to attract (a fresh workspace with isolated
  // notes still gets a tidy ring).
  const initialNodes = useMemo<Node<PlantNodeData>[]>(() => {
    const stored = loadPositions(workspaceId)
    // Same strategy as the resync effect: always run the simulator
    // when there are notes; the tag-spring edges in weightedLinks
    // give the canvas structure even before a single manual link
    // exists. seedLayout is the fallback when there's nothing to
    // simulate (empty notes).
    const seeded =
      notes.length > 0
        ? forceLayout(notes, weightedLinks, {
          centrality: centralityMap,
          clusterOf: clusterKeyFor,
        })
        : seedLayout(notes, primaryTagFor)
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
          centrality: null,
          walkStep: null,
          isWalkSeed: false,
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

  // Track when the force layout has been applied with the current
  // (notes + links) combination so a links-arrived-after-notes race
  // doesn't leave the canvas stuck on a seedLayout pile-up. We
  // re-simulate any time the signature changes; user-dragged
  // positions still win at the per-node merge below (stored takes
  // precedence over the simulator's coordinates).
  const lastForceSig = useRef('')

  // Sync nodes when the notes set changes (focus filter toggled,
  // new note created, etc.). Preserves any in-memory user drag
  // positions for notes that survive across the change. The force
  // layout runs when the (notes, links) shape changes; the
  // signature gate avoids re-running the simulator on every minor
  // re-render (e.g. a single tag toggle).
  useEffect(() => {
    const stored = loadPositions(workspaceId)
    // Re-run the layout when the link shape changes AND when the
    // async analytics arrive: ``c<n>`` flips once PageRank centrality
    // loads, ``k<n>`` once Leiden clusters load, so the first paint's
    // centrality-blind glades get re-laid once the real signal is in.
    const sig = `force:${notes.length}:c${centralityMap.size}:k${
      Object.keys(clusters).length
    }:${weightedLinks
      .map((l) => `${l.source}>${l.target}`)
      .sort()
      .join('|')}`
    // Always run the simulator on a signature change. With zero
    // edges the golden-angle spiral + repulsion still produces a
    // breathable spread; the tag-derived springs (folded into
    // ``weightedLinks`` above) organise notes that share generic
    // tags. seedLayout is kept only as a deterministic initial
    // pose when the simulator has nothing to converge towards
    // (truly empty notes set).
    const shouldForceLayout =
      notes.length > 0 && lastForceSig.current !== sig
    if (shouldForceLayout) lastForceSig.current = sig
    setNodes((current) => {
      const byId = new Map(current.map((c) => [c.id, c]))
      const seeded = shouldForceLayout
        ? forceLayout(notes, weightedLinks, {
          centrality: centralityMap,
          clusterOf: clusterKeyFor,
        })
        : seedLayout(notes, primaryTagFor)
      return notes.map((n) => {
        const existing = byId.get(n.id)
        // When the force layout just (re-)ran for this signature,
        // its coordinates beat the stale in-memory position from a
        // previous seedLayout pass — otherwise we'd freeze the
        // canvas on the pre-links pile-up. User-dragged positions
        // (``stored``) still win regardless.
        const pos = shouldForceLayout
          ? (stored[n.id] ?? seeded[n.id] ?? existing?.position ?? { x: 0, y: 0 })
          : (existing?.position ?? stored[n.id] ?? seeded[n.id] ?? { x: 0, y: 0 })
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
            centrality: showCentrality ? (centrality[n.id] ?? 0) : null,
            walkStep: walkPath[n.id] ?? null,
            isWalkSeed: walkSeed === n.id,
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
    weightedLinks,
    showCentrality,
    centrality,
    centralityMap,
    clusters,
    clusterKeyFor,
    walkPath,
    walkSeed,
  ])

  // Persist positions ONLY on a real drag-end. The earlier
  // every-nodes-change save burned the initial seedLayout pile-up
  // into localStorage, then loaded it back on top of every
  // subsequent force layout forever -- the canvas was effectively
  // pinned to its first race-pose. Hook into the NodeChange stream
  // through ``handleNodesChange`` (below) and only call
  // ``savePositions`` when at least one change is a position update
  // whose dragging flag has flipped to false.
  const handleNodesChange = useCallback(
    (changes: NodeChange<Node<PlantNodeData>>[]) => {
      onNodesChange(changes)
      // setIsDragging(true) is a no-op re-render once true (React bails
      // on an unchanged value), so calling it per tick is cheap.
      if (
        changes.some(
          (c) => c.type === 'position' && 'dragging' in c && c.dragging === true,
        )
      ) {
        setIsDragging(true)
      }
      const dragEnded = changes.some(
        (c) =>
          c.type === 'position' &&
          'dragging' in c &&
          c.dragging === false,
      )
      if (!dragEnded) return
      setIsDragging(false)
      // Snapshot the freshest positions after the simulator and
      // user drag have all flushed. ``positionsRef`` is the
      // authoritative cache because the ``setNodes`` reducer
      // hasn't committed yet here.
      const snap: Record<string, { x: number; y: number }> = {}
      for (const n of nodes) {
        snap[n.id] = { x: n.position.x, y: n.position.y }
      }
      // Apply the drag delta from the change(s) to the snapshot so
      // the saved set is post-drop, not pre-drop.
      for (const c of changes) {
        if (c.type === 'position' && c.position && 'id' in c) {
          snap[c.id] = { x: c.position.x, y: c.position.y }
        }
      }
      positionsRef.current = snap
      savePositions(workspaceId, snap)
    },
    [nodes, onNodesChange, workspaceId],
  )

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

  // ---------------------------------------------------------------
  // Garden analytics (task 8c0a8f08 Phase 1): GET /garden/graph
  // returns PageRank centrality + materialised edge weights. Cheap
  // single round-trip; reload triggers piggyback on the same signal
  // we use for links so the two layers stay coherent.
  // ---------------------------------------------------------------
  const reloadGraph = useCallback(async () => {
    const res = await authFetch('/garden/graph')
    if (!res.ok) return
    const data = (await res.json()) as {
      edges: { src: string; dst: string; weight: number }[]
      centrality: Record<string, number>
    }
    setCentrality(data.centrality || {})
    const wmap: Record<string, number> = {}
    for (const e of data.edges) {
      const k = e.src < e.dst ? `${e.src}::${e.dst}` : `${e.dst}::${e.src}`
      wmap[k] = e.weight
    }
    setEdgeWeightMap(wmap)
    // Leiden communities (separate, heavier endpoint). Empty when the
    // clustering extra is absent; the layout then falls back to tag
    // glades.
    const cres = await authFetch('/garden/clusters')
    if (cres.ok) {
      const cdata = (await cres.json()) as { clusters: Record<string, number> }
      setClusters(cdata.clusters || {})
    }
  }, [])

  useEffect(() => {
    let active = true
    void (async () => {
      const res = await authFetch('/garden/graph')
      if (!active) return
      if (!res.ok) return
      const data = (await res.json()) as {
        edges: { src: string; dst: string; weight: number }[]
        centrality: Record<string, number>
      }
      if (!active) return
      setCentrality(data.centrality || {})
      const wmap: Record<string, number> = {}
      for (const e of data.edges) {
        const k = e.src < e.dst ? `${e.src}::${e.dst}` : `${e.dst}::${e.src}`
        wmap[k] = e.weight
      }
      setEdgeWeightMap(wmap)
      const cres = await authFetch('/garden/clusters')
      if (!active) return
      if (cres.ok) {
        const cdata = (await cres.json()) as { clusters: Record<string, number> }
        if (active) setClusters(cdata.clusters || {})
      }
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
        // Per-kind stroke vocabulary, modulated by edge weight v1
        // (task 7e99c724): the base width is the kind's signature
        // thickness; the extra millimeter comes from soft-OR(w_kind,
        // w_tag) so a link reinforced by tag overlap reads as a
        // stronger filament without changing the kind's identity
        // (dashed remains dashed, solid remains solid).
        const shared = sharedGenericTagCount(
          noteById.get(l.parent_note_id),
          noteById.get(l.child_note_id),
        )
        const w = edgeWeightV1(l.kind, shared)
        const widthBoost = 0.6 * Math.pow(w, 0.7)
        const style: CSSProperties =
          kind === 'hypha_of'
            ? // a solid living filament grown from another idea
              { stroke: color, strokeWidth: 2.2 + widthBoost }
            : kind === 'related'
              ? {
                  // the neutral associative thread: dashed, low-contrast
                  // so it reads as background weave, lifted off the
                  // lowest-contrast grey to stay visible on the canvas.
                  stroke: 'color-mix(in srgb, var(--moss) 45%, var(--muted))',
                  strokeWidth: 1.5 + widthBoost,
                  strokeDasharray: '3 3',
                  opacity: 0.8,
                }
              : kind === 'supersedes'
                ? { stroke: color, strokeWidth: 2.0 + widthBoost, opacity: 0.9 }
                : // contradicts: a struck filament (rust), dashed + arrow
                  {
                    stroke: color,
                    strokeWidth: 2.0 + widthBoost,
                    strokeDasharray: '5 3',
                    opacity: 0.95,
                  }
        const pairKey =
          l.parent_note_id < l.child_note_id
            ? `${l.parent_note_id}::${l.child_note_id}`
            : `${l.child_note_id}::${l.parent_note_id}`
        const materialisedW = edgeWeightMap[pairKey]
        // The kind name (and optional weight) used to be drawn on
        // every edge, which blanketed the canvas in overlapping label
        // rects. They now live in ``data`` and the focus overlay
        // surfaces them only for edges touching the hovered/pinned
        // node; the legend carries kind→colour at rest.
        return {
          id: `mm-link-${l.id}`,
          source: l.parent_note_id,
          target: l.child_note_id,
          type: 'default',
          animated: false,
          style,
          markerEnd:
            kind === 'supersedes' || kind === 'contradicts'
              ? { type: MarkerType.ArrowClosed, color, width: 14, height: 14 }
              : undefined,
          data: {
            kind: l.kind,
            linkId: l.id,
            isManual: true,
            kindLabel: t(`garden.mindmap.linkKind.${l.kind}`),
            weightLabel: materialisedW != null ? materialisedW.toFixed(2) : null,
          },
        }
      })
    const tagEdges: Edge[] = []
    if (showTagEdges) {
      const computed = buildTagEdges(notes, noteIds)
      // Cap fan-out: a popular generic tag pairs O(k^2) notes, which
      // is the bulk of the hairball. Keep only each node's top-K
      // strongest shared-tag links (rank by overlap, deterministic
      // tie-break by pair key), turning a clique into a sparse ring.
      const TOP_K = 3
      const pkey = (a: string, b: string) => (a < b ? `${a}::${b}` : `${b}::${a}`)
      const byNode = new Map<string, { key: string; strength: number }[]>()
      for (const e of computed) {
        const key = pkey(e.source, e.target)
        const strength = e.sharedTags.length
        for (const nid of [e.source, e.target]) {
          const arr = byNode.get(nid) ?? []
          arr.push({ key, strength })
          byNode.set(nid, arr)
        }
      }
      const keep = new Set<string>()
      for (const arr of byNode.values()) {
        arr.sort((a, b) => b.strength - a.strength || a.key.localeCompare(b.key))
        for (let i = 0; i < Math.min(TOP_K, arr.length); i++) keep.add(arr[i].key)
      }
      for (const e of computed) {
        if (!keep.has(pkey(e.source, e.target))) continue
        const n = e.sharedTags.length
        tagEdges.push({
          id: `mm-tag-${e.source}-${e.target}`,
          source: e.source,
          target: e.target,
          type: 'straight',
          // No label at rest (the focus overlay surfaces the tag
          // names on hover). One neutral hue so the constellation
          // never competes chromatically with the four kind colours,
          // and a low opacity so it reads as a backdrop, not a mesh.
          style: {
            stroke: 'color-mix(in srgb, var(--moss) 35%, transparent)',
            strokeWidth: 0.75,
            strokeDasharray: '4 3',
            opacity: Math.min(0.22, 0.1 + 0.04 * n),
          },
          data: {
            isManual: false,
            tagLabel: e.sharedTags.map((tg) => tg.name).join(' · '),
          },
          selectable: false,
        })
      }
    }
    // Illuminated trail: the pollinator's path overlaid on the graph.
    // free_wander visits nodes in sequence, so consecutive steps draw a
    // luminous, animated, directed filament on top of the base edges —
    // visible even when the underlying edge type (tag / manual) is
    // toggled off. focused walks are a PPR-ranked set with no traversal
    // order, so they light up nodes only (handled in PlantNode).
    const walkEdges: Edge[] = []
    if (walkResultMode === 'free_wander' && walkSeq.length > 1) {
      for (let i = 0; i < walkSeq.length - 1; i++) {
        const source = walkSeq[i]
        const target = walkSeq[i + 1]
        if (!source || !target || source === target) continue
        if (!noteIds.has(source) || !noteIds.has(target)) continue
        walkEdges.push({
          id: `mm-walk-${i}`,
          source,
          target,
          type: 'straight',
          animated: true,
          selectable: false,
          focusable: false,
          zIndex: 5,
          style: {
            stroke: 'var(--bloom)',
            strokeWidth: 3,
            opacity: 0.95,
          },
          markerEnd: {
            type: MarkerType.ArrowClosed,
            color: 'var(--bloom)',
            width: 16,
            height: 16,
          },
          data: { isManual: false, isWalk: true },
        })
      }
    }
    setEdges([...tagEdges, ...manual, ...walkEdges])
  }, [
    workspaceLinks,
    notes,
    noteIds,
    showTagEdges,
    edgeWeightMap,
    walkSeq,
    walkResultMode,
    t,
    setEdges,
    noteById,
  ])

  // Adjacency + edge-incidence index derived from the final edge set
  // (manual + any shown tag edges; the walk-trail overlay is excluded
  // since it duplicates real edges and free_wander can jump). Lets
  // hover/pin focus look up a node's neighbours and incident edges in
  // O(deg), never rescanning links on every mouse move.
  const graphIndex = useMemo(() => {
    const neighbors = new Map<string, Set<string>>()
    const incident = new Map<string, Set<string>>()
    const touch = (a: string, b: string, edgeId: string) => {
      let ns = neighbors.get(a)
      if (!ns) neighbors.set(a, (ns = new Set()))
      ns.add(b)
      let es = incident.get(a)
      if (!es) incident.set(a, (es = new Set()))
      es.add(edgeId)
    }
    for (const e of edges) {
      if (!e.source || !e.target) continue
      if ((e.data as { isWalk?: boolean } | undefined)?.isWalk) continue
      touch(e.source, e.target, e.id)
      touch(e.target, e.source, e.id)
    }
    return { neighbors, incident }
  }, [edges])

  // Focus+context overlay. When a node is hovered (transient) or
  // pinned (click to toggle), bring its incident edges + neighbours
  // forward and recede the rest. Render-only: it never rebuilds the
  // layout or the base edge set. Skipped (base arrays returned as-is)
  // when there is no focus or while a drag is in progress (a hover
  // during a drag would otherwise re-map all N nodes per tick); a
  // focus whose node has left the visible set is already neutralised
  // upstream by activeFocus's validity check.
  const displayNodes = useMemo(() => {
    if (!activeFocus || isDragging) return nodes
    const nbrs = graphIndex.neighbors.get(activeFocus)
    return nodes.map((n) => {
      // Walk-trail nodes and search hits stay lit even outside the
      // focused node's 1-hop neighbourhood: fading them would blank
      // the walk (a focused/PPR walk has no edges, only lit nodes)
      // and hide the note the user just searched for.
      const lit =
        nbrs?.has(n.id) ||
        n.data.walkStep != null ||
        n.data.isWalkSeed ||
        n.data.highlighted
      const focusState: 'focus' | 'neighbor' | 'faded' =
        n.id === activeFocus ? 'focus' : lit ? 'neighbor' : 'faded'
      return { ...n, data: { ...n.data, focusState } }
    })
  }, [nodes, activeFocus, graphIndex, isDragging])

  const displayEdges = useMemo(() => {
    if (!activeFocus || isDragging) return edges
    const inc = graphIndex.incident.get(activeFocus) ?? new Set<string>()
    return edges.map((e) => {
      const data = e.data as
        | {
            isManual?: boolean
            isWalk?: boolean
            kindLabel?: string
            weightLabel?: string | null
            tagLabel?: string
          }
        | undefined
      // The walk trail is its own emphasis layer; leave it alone.
      if (data?.isWalk) return e
      const base = (e.style ?? {}) as CSSProperties
      if (inc.has(e.id)) {
        const label = data?.isManual
          ? showEdgeWeights && data.weightLabel
            ? `${data.kindLabel} · ${data.weightLabel}`
            : data?.kindLabel
          : data?.tagLabel
        const bw = typeof base.strokeWidth === 'number' ? base.strokeWidth : 1.5
        return {
          ...e,
          className: 'mm-edge--focus',
          label,
          labelStyle: { fontSize: data?.isManual ? 10 : 9, fill: 'var(--text)' },
          labelBgStyle: { fill: 'var(--surface)', fillOpacity: 0.9 },
          style: { ...base, opacity: 1, strokeWidth: bw + 0.6 },
        }
      }
      return { ...e, className: 'mm-edge--faded', style: { ...base, opacity: 0.07 } }
    })
  }, [edges, activeFocus, graphIndex, isDragging, showEdgeWeights])

  // Re-plant: recompute the deterministic layout from scratch and
  // clear the persisted drag positions for this workspace (explicit,
  // confirm-gated) so a canvas that converged badly — or a pile burned
  // in by an old build — can be recovered. fitView reframes after.
  const replant = useCallback(() => {
    if (!window.confirm(t('garden.mindmap.replantConfirm'))) return
    try {
      localStorage.removeItem(positionsStorageKey(workspaceId))
    } catch {
      // ignore: storage disabled / quota; the recompute still applies
    }
    positionsRef.current = {}
    lastForceSig.current = ''
    const seeded = forceLayout(notes, weightedLinks, {
          centrality: centralityMap,
          clusterOf: clusterKeyFor,
        })
    setNodes((cur) =>
      cur.map((n) => ({ ...n, position: seeded[n.id] ?? n.position })),
    )
    window.setTimeout(() => rf.fitView({ padding: 0.2, maxZoom: 1.4 }), 60)
  }, [t, workspaceId, notes, weightedLinks, centralityMap, clusterKeyFor, setNodes, rf])

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
      setLinkKind('related')
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
    void reloadGraph()
  }, [pendingConnect, linkKind, reloadLinks, reloadGraph])

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
        void reloadGraph()
      })()
    },
    [reloadLinks, reloadGraph, t],
  )

  const onNodeClick = useCallback<NodeMouseHandler<Node<PlantNodeData>>>(
    (_event, node) => {
      // Single click does nothing destructive — opening the plant
      // modal is on double-click (PlantNode handles it directly).
      // Centering on the node mimics the search-hit behavior, AND
      // we mark the node as the walk seed (task 5bf31b63) so the
      // toolbar 'walk' button has a target.
      rf.setCenter(node.position.x, node.position.y, { duration: 200, zoom: 1.1 })
      setWalkSeed(node.id)
      // Toggle a pinned focus so the node's subgraph stays isolated
      // after the cursor leaves; click it again (or the canvas) to
      // release. Transient hover focus is handled separately.
      setPinnedId((prev) => (prev === node.id ? null : node.id))
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
        <label className="garden__mindmap-toggle">
          <input
            type="checkbox"
            checked={showCentrality}
            onChange={(e) => setShowCentrality(e.target.checked)}
          />
          <span>{t('garden.mindmap.showCentrality')}</span>
        </label>
        <label className="garden__mindmap-toggle">
          <input
            type="checkbox"
            checked={showEdgeWeights}
            onChange={(e) => setShowEdgeWeights(e.target.checked)}
          />
          <span>{t('garden.mindmap.showEdgeWeights')}</span>
        </label>
        <select
          value={walkMode}
          onChange={(e) =>
            setWalkMode(e.target.value === 'free_wander' ? 'free_wander' : 'focused')
          }
          aria-label={t('garden.mindmap.walkMode')}
        >
          <option value="focused">{t('garden.mindmap.walkFocused')}</option>
          <option value="free_wander">{t('garden.mindmap.walkFree')}</option>
        </select>
        <button
          type="button"
          className="btn--sm"
          disabled={!walkSeed}
          onClick={() => {
            if (!walkSeed) return
            void (async () => {
              const params = new URLSearchParams({
                seed: walkSeed,
                mode: walkMode,
                budget: '12',
              })
              const res = await authFetch(`/garden/walk?${params.toString()}`)
              if (!res.ok) {
                setErr(`HTTP ${res.status}`)
                return
              }
              const data = (await res.json()) as {
                steps: { note_id: string; step: number }[]
              }
              const sorted = [...data.steps].sort((a, b) => a.step - b.step)
              const next: Record<string, number> = {}
              // First visit wins the badge step so a revisited node keeps
              // a stable number; the full sequence (with revisits) lives
              // in walkSeq and drives the trail.
              for (const s of sorted) {
                if (!(s.note_id in next)) next[s.note_id] = s.step
              }
              setWalkPath(next)
              setWalkSeq(sorted.map((s) => s.note_id))
              setWalkResultMode(walkMode)
            })()
          }}
        >
          {t('garden.mindmap.walkRun')}
        </button>
        <button
          type="button"
          className="btn--ghost btn--sm"
          disabled={Object.keys(walkPath).length === 0}
          onClick={() => {
            setWalkPath({})
            setWalkSeq([])
          }}
        >
          {t('garden.mindmap.walkClear')}
        </button>
        <button
          type="button"
          className="btn--ghost btn--sm"
          onClick={replant}
          title={t('garden.mindmap.replantHint')}
        >
          {t('garden.mindmap.replant')}
        </button>
        {walkSeed && (
          <span className="muted">
            {t('garden.mindmap.walkSeed', {
              title: notes.find((n) => n.id === walkSeed)?.title || '·',
            })}
          </span>
        )}
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
        <div className="garden__mindmap-canvas" data-lod={lod}>
          <ReactFlow
            nodes={displayNodes}
            edges={displayEdges}
            onNodesChange={handleNodesChange}
            onEdgesChange={onEdgesChange}
            onConnect={onConnect}
            onEdgeClick={onEdgeClick}
            onNodeClick={onNodeClick}
            onNodeMouseEnter={(_, node) => setHoverId(node.id)}
            onNodeMouseLeave={() => setHoverId(null)}
            onPaneClick={() => setPinnedId(null)}
            onMove={(_, vp) => {
              const next = vp.zoom < 0.5 ? 'far' : 'near'
              setLod((cur) => (cur === next ? cur : next))
            }}
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
              <p className="hint garden__mindmap-linkdir">
                <strong>
                  {noteById.get(pendingConnect.source ?? '')?.title || '·'}
                </strong>
                {UNDIRECTED_KINDS.has(linkKind) ? ' ↔ ' : ' → '}
                <strong>
                  {noteById.get(pendingConnect.target ?? '')?.title || '·'}
                </strong>
                {!UNDIRECTED_KINDS.has(linkKind) && (
                  <button
                    type="button"
                    className="btn--ghost btn--sm"
                    title={t('garden.mindmap.swapDirection')}
                    onClick={() =>
                      setPendingConnect((c) =>
                        c ? { ...c, source: c.target, target: c.source } : c,
                      )
                    }
                  >
                    ⇄
                  </button>
                )}
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
              <p className="hint garden__mindmap-kindhint">
                {t(`garden.mindmap.linkKindHint.${linkKind}`)}
              </p>
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
