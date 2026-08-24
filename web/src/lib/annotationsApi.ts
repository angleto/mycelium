import { authFetch, errMessage } from '../api/client'
import type { DocKind } from './useAnnotations'

// Thin REST helpers for the annotation layer, shared by the inline
// editor UX (InlineAnnotator) and the overview panel. Each returns a
// discriminated result so callers surface the server message instead of
// a silent no-op. The endpoints mirror api/routers/annotations.py.

export interface ApiResult {
  ok: boolean
  error?: string
}

async function call(
  path: string,
  method: 'POST' | 'PATCH' | 'DELETE',
  payload?: Record<string, unknown>,
): Promise<ApiResult> {
  try {
    const res = await authFetch(path, {
      method,
      headers: payload ? { 'Content-Type': 'application/json' } : undefined,
      body: payload ? JSON.stringify(payload) : undefined,
    })
    if (!res.ok) {
      return { ok: false, error: errMessage(await res.json().catch(() => ({}))) }
    }
    return { ok: true }
  } catch (e) {
    return { ok: false, error: e instanceof Error ? e.message : String(e) }
  }
}

/** The projection an anchor's quote is written in (server: `anchor_domain`). */
export type AnchorDomain = 'source' | 'rendered'

export function createComment(args: {
  docKind: DocKind
  docId: string
  body: string
  anchorQuote?: string | null
  anchorPrefix?: string | null
  anchorSuffix?: string | null
  /** Which projection the quote is written in.
   *
   *  `'source'` is the markdown itself, and is the default everywhere: the
   *  markdown editor's document IS the source, so a selection is a source
   *  span. The retired WYSIWYG surface captured the RENDERED text (markup
   *  stripped, links reduced to their label, blocks joined by a space) and
   *  its rows say `'rendered'`, because a quote read in the wrong domain
   *  either fails to locate or matches the wrong passage. */
  anchorDomain?: AnchorDomain
  parentId?: string | null
}): Promise<ApiResult> {
  return call('/annotations/comment', 'POST', {
    doc_kind: args.docKind,
    doc_id: args.docId,
    body: args.body,
    anchor_quote: args.anchorQuote ?? null,
    anchor_prefix: args.anchorPrefix ?? null,
    anchor_suffix: args.anchorSuffix ?? null,
    anchor_domain: args.anchorDomain ?? 'source',
    parent_id: args.parentId ?? null,
  })
}

export function createSuggestion(args: {
  docKind: DocKind
  docId: string
  originalText: string
  proposedText: string
  rationale?: string
  anchorPrefix?: string | null
  anchorSuffix?: string | null
  /** Which projection the quote is written in.
   *
   *  `'source'` is the markdown itself, and is the default everywhere: the
   *  markdown editor's document IS the source, so a selection is a source
   *  span. The retired WYSIWYG surface captured the RENDERED text (markup
   *  stripped, links reduced to their label, blocks joined by a space) and
   *  its rows say `'rendered'`, because a quote read in the wrong domain
   *  either fails to locate or matches the wrong passage. */
  anchorDomain?: AnchorDomain
}): Promise<ApiResult> {
  return call('/annotations/suggestion', 'POST', {
    doc_kind: args.docKind,
    doc_id: args.docId,
    original_text: args.originalText,
    proposed_text: args.proposedText,
    rationale: args.rationale ?? '',
    anchor_prefix: args.anchorPrefix ?? null,
    anchor_suffix: args.anchorSuffix ?? null,
    anchor_domain: args.anchorDomain ?? 'source',
  })
}

/** accept | reject | resolve | reopen */
export function act(id: string, verb: string, expectedVersion: number): Promise<ApiResult> {
  return call(`/annotations/${id}/${verb}`, 'POST', { expected_version: expectedVersion })
}

export function editBody(id: string, body: string, expectedVersion: number): Promise<ApiResult> {
  return call(`/annotations/${id}`, 'PATCH', { body, expected_version: expectedVersion })
}

export function remove(id: string, expectedVersion: number): Promise<ApiResult> {
  return call(`/annotations/${id}?expected_version=${expectedVersion}`, 'DELETE')
}
