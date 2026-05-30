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

// Rendered-text projection of the document with a per-character map back
// to live ProseMirror positions, MIRRORING ``doc.textBetween(0, size,
// ' ')`` (the exact domain InlineAnnotator.readSelection captures an
// anchor in, and the exact domain the server's md_anchor resolves the
// splice in). So "what is struck" == "what accept will splice". Marks
// contribute only their text, links only their link-text, atoms (image,
// inline math) and hard breaks nothing, and text runs of different
// blocks are joined by a single space. ``pmPos[i]`` is the live PM
// position of rendered char ``i``.
interface DocMap {
  text: string
  pmPos: number[]
}

function renderDocWithMap(doc: PMNode): DocMap {
  let text = ''
  const pmPos: number[] = []
  // Mirrors prosemirror-model Fragment.textBetween: a block separator is
  // emitted on entering a block only when text has been produced since
  // the last one (``separated`` tracks that).
  let separated = true
  doc.nodesBetween(0, doc.content.size, (node, pos) => {
    if (node.isText && node.text) {
      const t = node.text
      for (let i = 0; i < t.length; i += 1) {
        text += t[i]
        pmPos.push(pos + i)
      }
      separated = false
    } else if (node.isLeaf) {
      // image / inlineMath / hardBreak: no text, no separator change.
    } else if (!separated && node.isBlock) {
      text += ' '
      pmPos.push(pos)
      separated = true
    }
    return true
  })
  return { text, pmPos }
}

// Locate ``needle`` in the rendered projection, preferring the occurrence
// bounded by the W3C ``prefix``/``suffix`` when supplied, skipping ranges
// already consumed by an earlier anchor so two annotations quoting the
// same text land on successive occurrences. Maps the rendered span back
// to a (possibly multi-node / multi-block / inside-mark) PM range. Returns
// null when nothing matches (the mark is simply not drawn — it still shows
// in the panel). Never throws.
function findRange(
  map: DocMap,
  needle: string,
  prefix: string | null,
  suffix: string | null,
  used: Range[],
): Range | null {
  if (!needle) return null
  const { text, pmPos } = map
  const pfx = prefix ?? ''
  const sfx = suffix ?? ''
  const anchored = pfx + needle + sfx
  const useAnchored = anchored !== needle
  const target = useAnchored ? anchored : needle
  let from = 0
  for (;;) {
    const idx = text.indexOf(target, from)
    if (idx < 0) break
    const s = idx + (useAnchored ? pfx.length : 0)
    const e = s + needle.length
    if (s >= 0 && e <= pmPos.length && e > s) {
      const cand: Range = { from: pmPos[s], to: pmPos[e - 1] + 1 }
      if (!overlaps(cand, used)) return cand
    }
    from = idx + 1
  }
  return null
}

function buildDeco(doc: PMNode, anchors: AnnotationAnchor[]): DecorationSet {
  const decos: Decoration[] = []
  const used: Range[] = []
  const map = renderDocWithMap(doc)
  for (const a of anchors) {
    if (a.status !== 'open') continue
    if (a.kind === 'comment') {
      if (!a.anchorQuote) continue
      const r = findRange(map, a.anchorQuote, a.anchorPrefix, a.anchorSuffix, used)
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
    const r = findRange(map, a.originalText, a.anchorPrefix, a.anchorSuffix, used)
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
