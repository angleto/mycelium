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
  /** Which projection the quote is written in. This layer resolves only
   *  RENDERED anchors -- its whole projection exists for them -- and the
   *  source layer (markdownSource/annotationLayer.ts) resolves only source
   *  ones. Neither guesses at the other's, because a quote read in the wrong
   *  domain can match the WRONG passage rather than simply missing. */
  anchorDomain?: 'source' | 'rendered'
}

interface Range {
  from: number
  to: number
}

interface AnnoState {
  anchors: AnnotationAnchor[]
  // Transient "go to text" highlight pulse over a located range, driven by
  // a meta transaction (annotationFlashKey) and cleared by the host after
  // ~1.5s. Rendered as a real PM decoration (NOT a hand-mutated DOM class,
  // which ProseMirror reverts on its next view reconciliation).
  flash: Range | null
  deco: DecorationSet
}

export const annotationKey = new PluginKey<AnnoState>('annotationDecorations')
// Meta channel for the flash pulse: payload is a {from,to} range, or null
// to clear. See RichEditor.flashRange / the panel's "go to text" + the
// toolbar's prev/next annotation navigation.
export const annotationFlashKey = new PluginKey<Range | null>('annotationFlash')

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

// Minimal anchor shape needed to locate an annotation's passage in the
// live prose (a structural subset of AnnotationAnchor).
export interface AnchorQuery {
  kind: 'comment' | 'suggestion'
  anchorQuote: string | null
  anchorPrefix: string | null
  anchorSuffix: string | null
  originalText: string | null
  anchorDomain?: 'source' | 'rendered'
}

// Live PM range of an annotation's anchored passage, computed with the
// SAME projection + match logic that draws the inline marks, so "where I
// jump" == "what is highlighted". Used by the panel's "go to text" action
// for annotations whose mark is not currently drawn (resolved comments,
// rejected suggestions): there is no decoration DOM to scroll to, so the
// host falls back to selecting this range. ``used`` is empty here, so it
// returns the FIRST prefix/suffix-disambiguated occurrence (the open-mark
// path queries the decoration's own DOM node instead, which is exact even
// when several annotations quote the same passage). Returns null when the
// passage no longer exists (e.g. an accepted suggestion spliced it away).
// If the prefix/suffix-anchored match fails but the quoted passage itself
// survived (only its surroundings changed, common once a doc is edited),
// it retries on the bare needle as a best-effort — fallback-path only, so
// buildDeco's strict matching for live marks is untouched.
export function locateAnchor(
  doc: PMNode,
  a: AnchorQuery,
): { from: number; to: number } | null {
  // A SOURCE anchor quotes the markdown, which this projection has stripped:
  // `**bold**` does not occur in it. Refuse rather than fall through to a
  // partial match on the bare word.
  if (a.anchorDomain === 'source') return null
  const needle = a.kind === 'suggestion' ? a.originalText : a.anchorQuote
  if (!needle) return null
  const map = renderDocWithMap(doc)
  return (
    findRange(map, needle, a.anchorPrefix, a.anchorSuffix, []) ??
    findRange(map, needle, null, null, [])
  )
}

function buildDeco(
  doc: PMNode,
  anchors: AnnotationAnchor[],
  flash: Range | null,
): DecorationSet {
  const decos: Decoration[] = []
  const used: Range[] = []
  // The flash pulse layers over whatever mark (if any) sits at the range;
  // PM merges the inline classes. ``anno-mark--flash`` carries the CSS
  // animation; it is its own decoration so it composes with — and is
  // independent of — the comment/suggestion marks (a resolved annotation
  // has no mark, yet still pulses).
  if (flash && flash.to > flash.from) {
    decos.push(Decoration.inline(flash.from, flash.to, { class: 'anno-mark--flash' }))
  }
  const map = renderDocWithMap(doc)
  for (const a of anchors) {
    if (a.status !== 'open') continue
    if (a.anchorDomain === 'source') continue
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
            return { anchors: initial, flash: null, deco: buildDeco(state.doc, initial, null) }
          },
          apply(tr, value, _old, newState) {
            const meta = tr.getMeta(annotationKey) as AnnotationAnchor[] | undefined
            const flashMeta = tr.getMeta(annotationFlashKey) as Range | null | undefined
            if (!tr.docChanged && meta === undefined && flashMeta === undefined) return value
            const anchors = meta ?? value.anchors
            // The flash range is for the current doc; a doc edit invalidates
            // it, so drop it rather than try to map a transient highlight.
            const flash = tr.docChanged ? null : flashMeta !== undefined ? flashMeta : value.flash
            return { anchors, flash, deco: buildDeco(newState.doc, anchors, flash) }
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
