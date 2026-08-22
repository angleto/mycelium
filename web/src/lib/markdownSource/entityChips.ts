import { syntaxTree } from '@codemirror/language'
import { StateEffect, StateField, type EditorState, type Extension, type Range } from '@codemirror/state'
import { Decoration, type DecorationSet, EditorView, ViewPlugin, type ViewUpdate } from '@codemirror/view'
import {
  getCachedLookup,
  isPrefixCandidate,
  lookupPrefix,
  type LookupMatch,
  type LookupOut,
} from '../prefixLookup'

// Clickable chips for a backticked UUID prefix (`91cf6aaa`), the ADR-0038
// convention that roadmap and planning notes are written in.
//
// The classification (`resolutionFromLookup`) and the cache (`prefixLookup`)
// are the ones the WYSIWYG version used and the read-side renderer uses.
// What changed is only the substrate: hits come from the markdown syntax
// tree instead of a ProseMirror walk, and the async resolution arrives as a
// StateEffect instead of a meta transaction.
//
// Navigation is deliberately NOT here. The decoration stamps
// `data-entity-prefix` and the global capture-phase click interceptor in
// AppShell resolves and routes it, exactly as it does for `@kind:id`
// mention anchors. One interceptor, no per-editor router wiring.

type Resolution = {
  state: 'loading' | 'resolved' | 'closed' | 'unresolved'
  title: string | null
}

/** One resolved prefix, pushed in when its lookup settles. */
const setResolution = StateEffect.define<{ prefix: string; res: Resolution }>()

type Hit = { from: number; to: number; prefix: string }

/**
 * Every inline code span whose content is a resolvable prefix.
 *
 * Over the WHOLE document rather than the viewport: the resolver pump below
 * warms the shared lookup cache, which is also what makes a click on a chip
 * navigate instantly, and a chip scrolled out of sight is exactly the one
 * whose lookup is worth having ready. The scan is cheap and only reruns when
 * the document changes.
 */
function collectHits(state: EditorState): Hit[] {
  const hits: Hit[] = []
  const doc = state.sliceDoc()
  syntaxTree(state).iterate({
    enter: (node) => {
      if (node.name !== 'InlineCode') return
      // Between the delimiters: the marks are the first and last children.
      let from = node.from
      let to = node.to
      for (let c = node.node.firstChild; c; c = c.nextSibling) {
        if (c.name !== 'CodeMark') continue
        if (c.from === node.from) from = c.to
        if (c.to === node.to) to = c.from
      }
      if (to <= from) return
      const raw = doc.slice(from, to)
      const trimmed = raw.trim()
      if (!isPrefixCandidate(trimmed)) return
      const lead = raw.length - raw.trimStart().length
      hits.push({
        from: from + lead,
        to: from + lead + trimmed.length,
        prefix: trimmed.toLowerCase(),
      })
    },
  })
  return hits
}

function resolutionFromLookup(res: LookupOut | null): Resolution {
  if (!res || res.matches.length === 0) return { state: 'unresolved', title: null }
  const primary: LookupMatch = res.matches.find((m) => m.kind === 'task') ?? res.matches[0]
  const closed = primary.kind === 'task' && primary.is_terminal === true
  return { state: closed ? 'closed' : 'resolved', title: primary.title?.trim() || null }
}

function build(hits: Hit[], resolved: Map<string, Resolution>): DecorationSet {
  const out: Range<Decoration>[] = []
  for (const h of hits) {
    const r = resolved.get(h.prefix)
    const cls = ['entity-ref']
    let title = h.prefix
    if (!r || r.state === 'loading') {
      cls.push('entity-ref--loading')
    } else if (r.state === 'unresolved') {
      cls.push('entity-ref--unresolved')
    } else {
      cls.push('entity-ref--resolved')
      if (r.state === 'closed') cls.push('entity-ref--closed')
      if (r.title) title = r.title
    }
    out.push(
      Decoration.mark({
        class: cls.join(' '),
        attributes: { title, 'data-entity-prefix': h.prefix },
      }).range(h.from, h.to),
    )
  }
  return Decoration.set(out, true)
}

type ChipState = { hits: Hit[]; resolved: Map<string, Resolution>; deco: DecorationSet }

const chipField = StateField.define<ChipState>({
  create(state) {
    const hits = collectHits(state)
    const resolved = new Map<string, Resolution>()
    return { hits, resolved, deco: build(hits, resolved) }
  },
  update(value, tr) {
    let resolved = value.resolved
    let touched = false
    for (const e of tr.effects) {
      if (!e.is(setResolution)) continue
      if (!touched) {
        resolved = new Map(resolved)
        touched = true
      }
      resolved.set(e.value.prefix, e.value.res)
    }
    if (!tr.docChanged && !touched) return value
    // Re-scan only when the text changed. A resolution arriving does not
    // move a chip, and re-parsing on every lookup would be pure waste.
    const hits = tr.docChanged ? collectHits(tr.state) : value.hits
    return { hits, resolved, deco: build(hits, resolved) }
  },
  provide: (f) => EditorView.decorations.from(f, (v) => v.deco),
})

/** Warms the shared lookup cache for every prefix in the document and feeds
 *  each answer back in. One request per distinct prefix per editor. */
const resolverPump = ViewPlugin.fromClass(
  class {
    private requested = new Set<string>()
    // Explicit field: this project builds with ``erasableSyntaxOnly``, which
    // rules out a parameter property.
    private readonly view: EditorView

    constructor(view: EditorView) {
      this.view = view
      this.pump()
    }

    update(u: ViewUpdate) {
      if (u.docChanged) this.pump()
    }

    private pump() {
      const st = this.view.state.field(chipField, false)
      if (!st) return
      for (const h of st.hits) {
        if (this.requested.has(h.prefix) || st.resolved.has(h.prefix)) continue
        this.requested.add(h.prefix)
        const deliver = (res: LookupOut | null) => {
          // The view may be gone by the time a lookup answers; dispatching
          // into a destroyed view throws.
          try {
            this.view.dispatch({
              effects: setResolution.of({ prefix: h.prefix, res: resolutionFromLookup(res) }),
            })
          } catch {
            // The editor was destroyed mid-flight. Nothing to update.
          }
        }
        const cached = getCachedLookup(h.prefix)
        if (cached) deliver(cached)
        else void lookupPrefix(h.prefix).then(deliver).catch(() => {})
      }
    }
  },
)

/** Clickable chips for backticked UUID prefixes. Presentational: it adds
 *  decorations and never dispatches a document change. */
export function entityChips(): Extension {
  return [chipField, resolverPump]
}
