import { useCallback, useEffect, useState } from 'react'

import { authFetch } from '../api/client'
import type { components } from '../api/schema'
import type { AnnotationAnchor } from './markdownSource/annotationLayer'

export type Annotation = components['schemas']['AnnotationOut']
export type DocKind = 'note_part' | 'task_description'

/** One-shot form prefill produced by an editor-selection action and
 * consumed by AnnotationsPanel. ``nonce`` re-triggers the panel effect
 * on repeated selections of the same text. */
export interface AnnotationPrefill {
  mode: 'comment' | 'suggest'
  quote: string
  prefix: string
  suffix: string
  nonce: number
}

/** Single source of truth for a markdown document's annotations: the
 * panel and the inline editor decorations share one fetch + reload, so
 * a mutation in the panel (resolve/accept/edit) refreshes the inline
 * marks too. */
export function useAnnotations(docKind: DocKind, docId: string) {
  const [rows, setRows] = useState<Annotation[]>([])
  const [error, setError] = useState('')

  const reload = useCallback(async () => {
    if (!docId) return
    const qs = new URLSearchParams({
      doc_kind: docKind,
      doc_id: docId,
      include_resolved: 'true',
    })
    try {
      const res = await authFetch(`/annotations?${qs.toString()}`)
      if (!res.ok) {
        setError(`HTTP ${res.status}`)
        return
      }
      setRows((await res.json()) as Annotation[])
      setError('')
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [docKind, docId])

  useEffect(() => {
    // Fetch-on-mount; reload() only setStates after its first await.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void reload()
  }, [reload])

  return { rows, reload, error }
}

/** Open annotations as inline-decoration anchors for the editor. */
/**
 * Which projection an anchor's quote is written in.
 *
 * Read defensively rather than off the generated type: `web/src/api/schema.d.ts`
 * has not been regenerated since `anchor_domain` was added server-side, and
 * regenerating it right now would sweep in another branch's in-flight API
 * changes. Anything that is not the literal 'rendered' reads as 'source',
 * which is also the server's default -- so a client running against a stale
 * schema degrades to the CORRECT answer rather than to undefined.
 */
export function anchorDomainOf(a: Annotation): 'source' | 'rendered' {
  return (a as { anchor_domain?: unknown }).anchor_domain === 'rendered'
    ? 'rendered'
    : 'source'
}

export function toAnchors(rows: Annotation[]): AnnotationAnchor[] {
  return rows
    .filter((r) => r.status === 'open' && !r.deleted_at)
    .map((r) => ({
      id: r.id,
      kind: r.kind === 'suggestion' ? 'suggestion' : 'comment',
      status: r.status,
      anchorQuote: r.anchor_quote ?? null,
      anchorPrefix: r.anchor_prefix ?? null,
      anchorSuffix: r.anchor_suffix ?? null,
      originalText: r.original_text ?? null,
      proposedText: r.proposed_text ?? null,
      anchorDomain: anchorDomainOf(r),
    }))
}
