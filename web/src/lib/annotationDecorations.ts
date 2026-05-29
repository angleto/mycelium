import { Extension } from '@tiptap/core'
import type { Node as PMNode } from '@tiptap/pm/model'
import { Plugin, PluginKey } from '@tiptap/pm/state'
import { Decoration, DecorationSet } from '@tiptap/pm/view'

// Inline rendering of the annotation layer inside the WYSIWYG RichEditor
// (the Google-Docs "suggesting" look): an open comment highlights its
// quoted passage; an open suggestion strikes its ``originalText`` in the
// live prose and shows the ``proposedText`` right after it in a distinct
// colour (a widget decoration).
//
// Like EntityPrefix this is PURELY presentational: it only adds
// ProseMirror decorations, never touches the document, the markdown
// round-trip or the selection, so it cannot regress the editor's
// fragile autosave/caret paths. The annotations themselves live in
// React state and are pushed in via a meta transaction
// (``setMeta(annotationKey, anchors)``) whenever they change.

export interface AnnotationAnchor {
  id: string
  kind: 'comment' | 'suggestion'
  status: string
  anchorQuote: string | null
  anchorPrefix: string | null
  anchorSuffix: string | null
  originalText: string | null
  proposedText: string | null
}

interface AnnoState {
  anchors: AnnotationAnchor[]
  deco: DecorationSet
}

export const annotationKey = new PluginKey<AnnoState>('annotationDecorations')

interface Range {
  from: number
  to: number
}

function overlaps(r: Range, used: Range[]): boolean {
  return used.some((u) => r.from < u.to && u.from < r.to)
}

// Locate ``needle`` within a single text node, preferring the occurrence
// bounded by the W3C-style ``prefix``/``suffix`` when supplied, and
// skipping ranges already consumed by an earlier anchor so two
// annotations quoting the same text land on successive occurrences
// rather than stacking on the first. Returns null when nothing
// matches (the mark is simply not drawn — it still shows in the panel).
// Quotes that span multiple nodes (formatting inside the run) don't
// decorate; this never throws.
function findRange(
  doc: PMNode,
  needle: string,
  prefix: string | null,
  suffix: string | null,
  used: Range[],
): Range | null {
  if (!needle) return null
  const pfx = prefix ?? ''
  const sfx = suffix ?? ''
  let hit: Range | null = null
  doc.descendants((node, pos) => {
    if (hit) return false
    if (!node.isText || !node.text) return undefined
    const text = node.text
    let from = 0
    for (;;) {
      const idx = text.indexOf(needle, from)
      if (idx < 0) break
      const cand: Range = { from: pos + idx, to: pos + idx + needle.length }
      const pOk = !pfx || text.slice(Math.max(0, idx - pfx.length), idx) === pfx
      const sOk = !sfx || text.slice(idx + needle.length, idx + needle.length + sfx.length) === sfx
      if (pOk && sOk && !overlaps(cand, used)) {
        hit = cand
        return false
      }
      from = idx + 1
    }
    return undefined
  })
  return hit
}

function buildDeco(doc: PMNode, anchors: AnnotationAnchor[]): DecorationSet {
  const decos: Decoration[] = []
  const used: Range[] = []
  for (const a of anchors) {
    if (a.status !== 'open') continue
    if (a.kind === 'comment') {
      if (!a.anchorQuote) continue
      const r = findRange(doc, a.anchorQuote, a.anchorPrefix, a.anchorSuffix, used)
      if (r) {
        used.push(r)
        decos.push(
          Decoration.inline(r.from, r.to, {
            class: 'anno-mark anno-mark--comment',
            'data-annotation-id': a.id,
          }),
        )
      }
      continue
    }
    // suggestion: strike the original, show the proposed replacement after it
    if (!a.originalText) continue
    const r = findRange(doc, a.originalText, a.anchorPrefix, a.anchorSuffix, used)
    if (!r) continue
    used.push(r)
    decos.push(
      Decoration.inline(r.from, r.to, {
        class: 'anno-mark anno-mark--del',
        'data-annotation-id': a.id,
      }),
    )
    const proposed = a.proposedText ?? ''
    if (proposed) {
      decos.push(
        Decoration.widget(
          r.to,
          () => {
            const span = document.createElement('span')
            span.className = 'anno-mark anno-mark--ins'
            span.textContent = proposed
            span.setAttribute('data-annotation-id', a.id)
            return span
          },
          // The key folds in the proposed text: ProseMirror reuses a
          // widget's DOM whenever the key matches without re-running the
          // builder, so an edited proposal must change the key or the
          // ghost text would stay stale.
          { side: 1, key: `anno-ins-${a.id}-${proposed}` },
        ),
      )
    }
  }
  return DecorationSet.create(doc, decos)
}

export const AnnotationDecorations = Extension.create<{ anchors: AnnotationAnchor[] }>({
  name: 'annotationDecorations',
  addOptions() {
    return { anchors: [] }
  },
  addProseMirrorPlugins() {
    const initial = this.options.anchors
    return [
      new Plugin<AnnoState>({
        key: annotationKey,
        state: {
          init(_config, state) {
            return { anchors: initial, deco: buildDeco(state.doc, initial) }
          },
          apply(tr, value, _old, newState) {
            const meta = tr.getMeta(annotationKey) as AnnotationAnchor[] | undefined
            if (!tr.docChanged && meta === undefined) return value
            const anchors = meta ?? value.anchors
            return { anchors, deco: buildDeco(newState.doc, anchors) }
          },
        },
        props: {
          decorations(state) {
            return annotationKey.getState(state)?.deco ?? null
          },
        },
      }),
    ]
  },
})
