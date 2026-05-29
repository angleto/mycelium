import { useCallback, useEffect, useState } from 'react'

import { authFetch } from '../api/client'
import type { components } from '../api/schema'
import type { AnnotationAnchor } from './annotationDecorations'

export type Annotation = components['schemas']['AnnotationOut']
export type DocKind = 'note_part' | 'task_description'

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
    }))
}
