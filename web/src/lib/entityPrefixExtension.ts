import { Extension } from '@tiptap/core'
import { Plugin, PluginKey } from '@tiptap/pm/state'
import { Decoration, DecorationSet } from '@tiptap/pm/view'
import type { Node as PMNode } from '@tiptap/pm/model'
import {
  getCachedLookup,
  isPrefixCandidate,
  lookupPrefix,
  type LookupMatch,
  type LookupOut,
} from './prefixLookup'

// Decorates inline ``code`` spans whose text is a UUID-prefix (the
// backtick convention from ADR-0038, e.g. `91cf6aaa`) so roadmap /
// planning notes read inside the WYSIWYG RichEditor get the same
// clickable affordance that MarkdownView already gives the read-only
// surfaces (conversation turns, garden preview).
//
// This extension is PURELY presentational: it adds prosemirror
// decorations over the existing inline code and never touches the
// document, the markdown round-trip, or the selection. That matters
// because those are the historically fragile parts of the editor
// (caret jumps on autosave, non-idempotent markdown round-trip). A
// decoration plugin cannot regress any of them.
//
// Navigation is NOT handled here. The decoration stamps a
// ``data-entity-prefix`` attribute on the span; the global
// capture-phase click interceptor in AppShell resolves it and routes,
// exactly as it already does for the ``@kind:id`` mention anchors. One
// interceptor, no per-editor router wiring.

type Resolution = {
  state: 'loading' | 'resolved' | 'closed' | 'unresolved'
  title: string | null
}

interface PrefixHit {
  from: number
  to: number
  prefix: string
}

interface PrefixState {
  hits: PrefixHit[]
  resolved: Map<string, Resolution>
  deco: DecorationSet
}

const key = new PluginKey<PrefixState>('entityPrefix')

// Walk the doc and collect every inline ``code`` run whose (trimmed)
// text is a resolvable UUID-prefix. A code-marked run is a single text
// node, so the whole token is matched against the anchored regex — a
// span like `git log` or `bge-m3` never matches (space / non-hex).
function collectHits(doc: PMNode): PrefixHit[] {
  const hits: PrefixHit[] = []
  doc.descendants((node, pos) => {
    if (!node.isText || !node.text) return
    if (!node.marks.some((m) => m.type.name === 'code')) return
    const raw = node.text
    const trimmed = raw.trim()
    if (!isPrefixCandidate(trimmed)) return
    const lead = raw.length - raw.trimStart().length
    const from = pos + lead
    hits.push({ from, to: from + trimmed.length, prefix: trimmed.toLowerCase() })
  })
  return hits
}

function resolutionFromLookup(res: LookupOut | null): Resolution {
  if (!res || res.matches.length === 0) {
    return { state: 'unresolved', title: null }
  }
  const primary: LookupMatch =
    res.matches.find((m) => m.kind === 'task') ?? res.matches[0]
  const closed = primary.kind === 'task' && primary.is_terminal === true
  return {
    state: closed ? 'closed' : 'resolved',
    title: primary.title?.trim() || null,
  }
}

function buildDeco(
  doc: PMNode,
  st: Pick<PrefixState, 'hits' | 'resolved'>,
): DecorationSet {
  const decos = st.hits.map((h) => {
    const r = st.resolved.get(h.prefix)
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
    return Decoration.inline(h.from, h.to, {
      class: cls.join(' '),
      title,
      'data-entity-prefix': h.prefix,
    })
  })
  return DecorationSet.create(doc, decos)
}

export const EntityPrefix = Extension.create({
  name: 'entityPrefix',
  addProseMirrorPlugins() {
    return [
      new Plugin<PrefixState>({
        key,
        state: {
          init(_config, state) {
            const hits = collectHits(state.doc)
            const resolved = new Map<string, Resolution>()
            return { hits, resolved, deco: buildDeco(state.doc, { hits, resolved }) }
          },
          apply(tr, value, _old, newState) {
            const meta = tr.getMeta(key) as
              | { prefix: string; res: Resolution }
              | undefined
            if (!tr.docChanged && !meta) return value
            let resolved = value.resolved
            if (meta) {
              resolved = new Map(value.resolved)
              resolved.set(meta.prefix, meta.res)
            }
            const hits = tr.docChanged ? collectHits(newState.doc) : value.hits
            return {
              hits,
              resolved,
              deco: buildDeco(newState.doc, { hits, resolved }),
            }
          },
        },
        props: {
          decorations(state) {
            return key.getState(state)?.deco ?? null
          },
        },
        // Resolver loop: for each distinct prefix in the doc, warm the
        // shared lookup cache once and re-decorate with the resolved
        // title / open-closed-unresolved state. Warming the cache here
        // also makes the AppShell click navigation instant.
        view(editorView) {
          const requested = new Set<string>()
          const pump = () => {
            const st = key.getState(editorView.state)
            if (!st) return
            for (const h of st.hits) {
              if (requested.has(h.prefix) || st.resolved.has(h.prefix)) continue
              requested.add(h.prefix)
              const dispatch = (res: LookupOut | null) => {
                if (editorView.isDestroyed) return
                editorView.dispatch(
                  editorView.state.tr.setMeta(key, {
                    prefix: h.prefix,
                    res: resolutionFromLookup(res),
                  }),
                )
              }
              const cached = getCachedLookup(h.prefix)
              if (cached) dispatch(cached)
              else void lookupPrefix(h.prefix).then(dispatch).catch(() => {})
            }
          }
          pump()
          return { update: pump }
        },
      }),
    ]
  },
})
