import { useCallback, useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { authFetch, errMessage } from '../api/client'
import { RichEditor } from './RichEditor'
import { MarkdownView } from './Markdown'
import type { ImageUploadParent } from '../lib/imageUpload'
import type { Annotation, AnnotationPrefill, DocKind } from '../lib/useAnnotations'

// The inline annotation layer (comments + suggestions) for a single
// markdown document, addressed by the generic (docKind, docId) handle.
// Controlled: ``rows`` + ``reload`` come from the shared useAnnotations
// hook, so the panel and the editor's inline decorations stay in sync
// (a resolve/accept here refreshes the marks too). A suggestion shows
// the struck original + the proposed replacement; the canonical
// markdown is untouched until a suggestion is accepted.
interface Props {
  docKind: DocKind
  docId: string
  rows: Annotation[]
  reload: () => Promise<void>
  /** Surfaced load error from the shared hook (a failed GET must not
   * look like "no annotations"). */
  loadError?: string
  /** Called after a mutation that changes the document body (accepting a
   * suggestion) so the host can refetch the prose. */
  onDocMutated?: () => void | Promise<void>
  /** One-shot prefill from an editor selection (the Comment / Suggest
   * edit toolbar actions): opens + fills the matching form. */
  prefill?: AnnotationPrefill
  /** Heading; e.g. "Work diary" for a task description. */
  title?: string
  /** Whether the suggestion composer is offered (only where the document
   * has editable prose to splice into). Defaults to true. */
  allowSuggest?: boolean
  /** Owning note/task. Threaded into the comment/reply/edit RichEditor so
   * an image pasted/uploaded into a comment lands on the right entity,
   * and into MarkdownView so a comment's `![alt](file)` reference resolves
   * against that entity's attachments. */
  imageUploadParent?: ImageUploadParent
  /** Scroll the sibling editor to an annotation's anchored passage. Wired
   * by the host (PartAnnotated / TaskDetailRoute) to the RichEditor's
   * viewRef handle. When omitted, the per-card "go to text" button is
   * hidden. Returns false when there was nothing to jump to (the passage
   * was edited away, or the anchor does not resolve in the source) so the
   * panel can show a brief hint. */
  onJumpToAnchor?: (a: Annotation) => boolean
}

function shortId(id: string | null | undefined): string {
  return id ? id.slice(0, 8) : '—'
}

function firstNonEmptyLine(s: string): string {
  for (const line of (s || '').split('\n')) {
    const t = line.trim()
    if (t) return t
  }
  return ''
}

function truncate(s: string, n: number): string {
  return s.length <= n ? s : s.slice(0, n - 1) + '…'
}

// One-line teaser shown in the head of a collapsed card, like the
// note-part preview: the comment's first line, or the suggestion's
// original → proposed when there is no rationale text.
function cardPreview(a: Annotation): string {
  const body = firstNonEmptyLine(a.body ?? '')
  if (body) return body
  if (a.kind === 'suggestion')
    return `${a.original_text ?? ''} → ${a.proposed_text ?? ''}`
  return ''
}

// ``uiBusy`` sentinel for the panel-wide collapse-all / expand-all
// request (mirrors NotePartsEditor's ALL_PARTS): no card id collides
// with it. A single-card toggle gates only its own chevron; the bulk
// request freezes every chevron until it lands.
const ALL_CARDS = '__all_cards__'

// The card badges the AUTHOR (a name, not a raw id) and, separately, the
// comment's OWN id. Before, the only badge was shortId(author_identity_id),
// which is the author id: it repeats across every comment by the same author
// and was mistaken for a useless comment id (task 515e13fb).
function authorLabel(a: Annotation): string {
  return a.author_label || a.author_handle || shortId(a.author_identity_id)
}
function authorTitle(a: Annotation): string {
  const who = a.author_handle ? `@${a.author_handle}` : (a.author_identity_id ?? '')
  return a.author_kind === 'ai_assistant' ? `${who} (AI)` : who
}

export function AnnotationsPanel({
  docKind,
  docId,
  rows,
  reload,
  loadError,
  onDocMutated,
  prefill,
  title,
  allowSuggest = true,
  imageUploadParent,
  onJumpToAnchor,
}: Props) {
  const { t } = useTranslation()
  const [err, setErr] = useState('')
  // Resolved comments and accepted/rejected suggestions are hidden by
  // default: on a heavily-annotated doc the open ones are what needs
  // attention, and the toggle brings the rest back when wanted.
  const [includeResolved, setIncludeResolved] = useState(false)
  // Accept/reject/resolve/reopen are async (accept also splices the body
  // server-side then refetches the prose, which takes a beat): track the
  // in-flight ``${id}:${verb}`` so the buttons disable + spin instead of
  // letting a double-click fire the mutation twice.
  const [pending, setPending] = useState<string | null>(null)
  // Suggestions whose last Accept came back SUGGESTION_STALE (the target
  // text can no longer be faithfully located in the live body): flagged
  // per-card so the user sees WHICH one failed — instead of one generic
  // line at the panel top — and gets the manual-apply affordance.
  const [staleIds, setStaleIds] = useState<Set<string>>(new Set())
  // Id of the card whose last "go to text" found no target (passage edited
  // away, or an anchor that does not resolve in the source): shows a brief
  // hint, auto-clears.
  const [jumpMiss, setJumpMiss] = useState<string | null>(null)
  const missTimer = useRef<number | null>(null)
  useEffect(
    () => () => {
      if (missTimer.current !== null) window.clearTimeout(missTimer.current)
    },
    [],
  )

  const jumpTo = (a: Annotation) => {
    setJumpMiss(null)
    if (onJumpToAnchor?.(a) === false) {
      setJumpMiss(a.id)
      if (missTimer.current !== null) window.clearTimeout(missTimer.current)
      missTimer.current = window.setTimeout(() => {
        setJumpMiss(null)
        missTimer.current = null
      }, 3000)
    }
  }

  // Per-card collapse, persisted server-side per user (annotation_ui_state,
  // like note parts): each row carries its ``ui_collapsed`` and a toggle
  // PUTs then reloads, so a folded thread stays folded on reopen — on any
  // device. ``uiBusy`` gates the double-click per card (its id, or the
  // ALL_CARDS sentinel while the header's collapse-all is in flight, which
  // freezes every chevron). ``displayed`` is the state the user SEES (an
  // open editor overrides the persisted fold), so the toggle always moves
  // away from what is on screen — not from a hidden server value.
  const [uiBusy, setUiBusy] = useState<string | null>(null)
  const toggleCollapsed = async (a: Annotation, displayed: boolean) => {
    if (uiBusy === a.id || uiBusy === ALL_CARDS) return
    setUiBusy(a.id)
    try {
      const { ok } = await send(`/annotations/${a.id}/ui-state`, 'PUT', {
        collapsed: !displayed,
      })
      if (ok) await reload()
    } finally {
      setUiBusy((cur) => (cur === a.id ? null : cur))
    }
  }
  const setAllCollapsed = async (collapsed: boolean) => {
    if (uiBusy === ALL_CARDS) return
    setUiBusy(ALL_CARDS)
    try {
      const { ok } = await send('/annotations/ui-state', 'PUT', {
        doc_kind: docKind,
        doc_id: docId,
        collapsed,
      })
      if (ok) await reload()
    } finally {
      setUiBusy((cur) => (cur === ALL_CARDS ? null : cur))
    }
  }

  // Card DOM handles for the in-card start/end jump buttons on the sticky
  // action bar (long comments: the bar is always visible, these bring you
  // back to the top or bottom of the card it belongs to).
  const cardRefs = useRef<Record<string, HTMLLIElement | null>>({})
  const jumpCard = (id: string, where: 'start' | 'end') =>
    cardRefs.current[id]?.scrollIntoView({ behavior: 'smooth', block: where })

  const [body, setBody] = useState('')
  const [quote, setQuote] = useState('')
  const [showSuggest, setShowSuggest] = useState(false)
  const [sugOrig, setSugOrig] = useState('')
  const [sugProp, setSugProp] = useState('')
  const [sugWhy, setSugWhy] = useState('')

  // Comment id copied-to-clipboard flash (per-card), so the now-visible
  // comment id is one click to grab (e.g. for delete_comment / a permalink).
  const [idCopied, setIdCopied] = useState<string | null>(null)
  const copyId = async (id: string) => {
    try {
      await navigator.clipboard.writeText(id)
      setIdCopied(id)
      window.setTimeout(() => setIdCopied(null), 1500)
    } catch {
      /* clipboard unavailable (insecure context) — non-fatal */
    }
  }

  // A NON-body mutation (resolve/reopen/accept/reject, or delete) that lost
  // the optimistic race (someone else — a teammate, or an MCP agent — saved
  // since this card was read). Reload so the card shows the current version,
  // then explain. Safe to reload here: no editor is open hiding the change,
  // and these actions do not blind-overwrite the body (task 515e13fb).
  const onStaleConflict = async () => {
    await reload()
    setErr(
      t('annotations.staleConflict', {
        defaultValue:
          'This comment changed since you opened it (someone else saved). The card now shows the current version — review it and try again if needed.',
      }),
    )
  }

  // An edit that lost the race is different: the editor is open holding the
  // user's full-body draft, so reloading alone would HIDE the concurrent text
  // and let a one-click re-save clobber it (lost update). Instead close the
  // editor (so the card renders the CURRENT body) and stash the draft, so the
  // user sees the other change first and re-applies their draft on top.
  const [conflictDraft, setConflictDraft] = useState<{ id: string; body: string } | null>(null)
  const onEditConflict = async (a: Annotation) => {
    setConflictDraft({ id: a.id, body: editBody })
    setEditId(null)
    setEditBody('')
    await reload()
    setErr(
      t('annotations.staleEditConflict', {
        defaultValue:
          'Someone else saved this comment since you opened it. The card now shows their version; your unsaved draft was kept — click “Re-apply my draft” to merge it on top, then save.',
      }),
    )
  }

  const [replyTo, setReplyTo] = useState<string | null>(null)
  const [replyBody, setReplyBody] = useState('')
  const [editId, setEditId] = useState<string | null>(null)
  const [editBody, setEditBody] = useState('')
  // Carried from an editor-selection prefill onto the next create.
  const [anchorPrefix, setAnchorPrefix] = useState('')
  const [anchorSuffix, setAnchorSuffix] = useState('')

  // Prefill + open the matching form when the host hands us an editor
  // selection (the Comment / Suggest edit toolbar actions).
  useEffect(() => {
    if (!prefill) return
    // Deriving form state from a one-shot prop trigger; intentional.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setAnchorPrefix(prefill.prefix)
    setAnchorSuffix(prefill.suffix)
    if (prefill.mode === 'suggest') {
      setShowSuggest(true)
      setSugOrig(prefill.quote)
    } else {
      setQuote(prefill.quote)
    }
  }, [prefill])

  const send = useCallback(
    async (
      path: string,
      method: 'POST' | 'PUT' | 'PATCH' | 'DELETE',
      payload?: Record<string, unknown>,
    ): Promise<{ ok: boolean; code: string | null }> => {
      const res = await authFetch(path, {
        method,
        headers: payload ? { 'Content-Type': 'application/json' } : undefined,
        body: payload ? JSON.stringify(payload) : undefined,
      })
      if (!res.ok) {
        const errBody = await res.json().catch(() => ({}))
        setErr(errMessage(errBody))
        return { ok: false, code: (errBody as { code?: string }).code ?? null }
      }
      setErr('')
      return { ok: true, code: null }
    },
    [],
  )

  const addComment = async () => {
    if (!body.trim()) return
    const { ok } = await send('/annotations/comment', 'POST', {
      doc_kind: docKind,
      doc_id: docId,
      body,
      anchor_quote: quote || null,
      anchor_prefix: anchorPrefix || null,
      anchor_suffix: anchorSuffix || null,
    })
    if (ok) {
      setBody('')
      setQuote('')
      setAnchorPrefix('')
      setAnchorSuffix('')
      await reload()
    }
  }

  const addSuggestion = async () => {
    if (!sugOrig.trim()) return
    const { ok } = await send('/annotations/suggestion', 'POST', {
      doc_kind: docKind,
      doc_id: docId,
      original_text: sugOrig,
      proposed_text: sugProp,
      rationale: sugWhy,
      anchor_prefix: anchorPrefix || null,
      anchor_suffix: anchorSuffix || null,
    })
    if (ok) {
      setSugOrig('')
      setSugProp('')
      setSugWhy('')
      setAnchorPrefix('')
      setAnchorSuffix('')
      setShowSuggest(false)
      await reload()
    }
  }

  const act = async (a: Annotation, verb: string) => {
    if (pending) return
    setPending(`${a.id}:${verb}`)
    try {
      const { ok, code } = await send(`/annotations/${a.id}/${verb}`, 'POST', {
        expected_version: a.version,
      })
      if (ok) {
        // A later successful action clears any prior stale flag on the card.
        setStaleIds((s) => {
          if (!s.has(a.id)) return s
          const n = new Set(s)
          n.delete(a.id)
          return n
        })
        await reload()
        // Accepting a suggestion splices the proposed text into the body
        // server-side; ask the host to refetch the prose so the editor
        // reflects it (resolve/reopen/reject leave the body untouched).
        if (verb === 'accept') await onDocMutated?.()
      } else if (verb === 'accept' && code === 'annotation.suggestion_stale') {
        // The proposed edit can no longer be located faithfully in the live
        // body (drifted or ambiguous): flag THIS card so the failure is
        // attributable, not just a generic line at the panel top.
        setStaleIds((s) => new Set(s).add(a.id))
      } else if (code === 'concurrency.stale_version') {
        await onStaleConflict()
      }
    } finally {
      setPending(null)
    }
  }

  // Action button (accept/reject/resolve/reopen) with an in-flight spinner.
  // Disables every act button on the card while one is running, and shows
  // the spinner on the one that was clicked, so the click clearly "took".
  const actBtn = (a: Annotation, verb: string, label: string, ghost = false) => {
    const busy = pending === `${a.id}:${verb}`
    return (
      <button
        type="button"
        className={`btn--sm${ghost ? ' btn--ghost' : ''}`}
        disabled={pending !== null}
        aria-busy={busy}
        onClick={() => void act(a, verb)}
      >
        {busy && <span className="btn-spinner" aria-hidden="true" />}
        {label}
      </button>
    )
  }

  const sendReply = async (parent: Annotation) => {
    if (!replyBody.trim()) return
    const { ok } = await send('/annotations/comment', 'POST', {
      doc_kind: docKind,
      doc_id: docId,
      body: replyBody,
      parent_id: parent.id,
    })
    if (ok) {
      setReplyTo(null)
      setReplyBody('')
      await reload()
    }
  }

  const saveEdit = async (a: Annotation) => {
    if (!editBody.trim()) return
    const { ok, code } = await send(`/annotations/${a.id}`, 'PATCH', {
      body: editBody,
      expected_version: a.version,
    })
    if (ok) {
      setEditId(null)
      setEditBody('')
      setConflictDraft((d) => (d?.id === a.id ? null : d))
      await reload()
    } else if (code === 'concurrency.stale_version') {
      await onEditConflict(a)
    }
  }

  const remove = async (a: Annotation) => {
    if (
      !confirm(
        t('annotations.confirmDelete', { defaultValue: 'Remove this annotation?' }),
      )
    )
      return
    const { ok, code } = await send(
      `/annotations/${a.id}?expected_version=${a.version}`,
      'DELETE',
    )
    if (ok) await reload()
    else if (code === 'concurrency.stale_version') await onStaleConflict()
  }

  // A root stays visible when it (or any of its replies) is still open,
  // so unchecking "show resolved" never hides live discussion under a
  // resolved parent.
  const roots = rows.filter(
    (r) =>
      !r.parent_id &&
      (includeResolved ||
        r.status === 'open' ||
        rows.some((x) => x.parent_id === r.id && x.status === 'open')),
  )
  const repliesOf = (id: string) =>
    rows.filter(
      (r) => r.parent_id === id && (includeResolved || r.status === 'open'),
    )

  const renderCard = (a: Annotation, isReply: boolean) => {
    const open = a.status === 'open'
    const isSuggestion = a.kind === 'suggestion'
    const editing = editId === a.id
    // An open editor wins over the persisted fold: clicking Edit on a
    // collapsed card must show the editor without a second click (the
    // server-side ui_collapsed is untouched). A root also stays visually
    // expanded while one of its REPLIES holds an open editor or a kept
    // conflict draft — collapsing it would unmount that editor mid-edit.
    const liveChild =
      !isReply &&
      rows.some(
        (x) => x.parent_id === a.id && (editId === x.id || conflictDraft?.id === x.id),
      )
    const collapsed = !!a.ui_collapsed && !editing && !liveChild
    // Count what expanding will actually show (repliesOf honours the
    // "Show resolved" filter), so the badge never over-promises.
    const replyCount = isReply ? 0 : repliesOf(a.id).length
    return (
      <li
        key={a.id}
        ref={(el) => {
          cardRefs.current[a.id] = el
        }}
        className={`anno anno--${a.kind} anno--${a.status}${collapsed ? ' anno--collapsed' : ''}`}
      >
        <div className="anno__head">
          <button
            type="button"
            className="anno__toggle"
            disabled={uiBusy === a.id || uiBusy === ALL_CARDS}
            onClick={() => void toggleCollapsed(a, collapsed)}
            aria-expanded={!collapsed}
            aria-label={
              collapsed
                ? t('annotations.expand', { defaultValue: 'Expand' })
                : t('annotations.collapse', { defaultValue: 'Collapse' })
            }
            title={
              collapsed
                ? t('annotations.expand', { defaultValue: 'Expand' })
                : t('annotations.collapse', { defaultValue: 'Collapse' })
            }
          >
            <span aria-hidden="true">{collapsed ? '▸' : '▾'}</span>
          </button>
          <span
            className={`anno__author${a.author_kind === 'ai_assistant' ? ' anno__author--ai' : ''}`}
            title={authorTitle(a)}
          >
            {authorLabel(a)}
          </span>
          <code
            className="anno__id"
            title={
              idCopied === a.id
                ? t('annotations.idCopied', { defaultValue: 'Comment id copied' })
                : t('annotations.copyId', { defaultValue: 'Copy comment id' })
            }
            onClick={() => void copyId(a.id)}
          >
            {shortId(a.id)}
          </code>
          <span className={`anno__status anno__status--${a.status}`}>{a.status}</span>
          {a.anchor_quote && !isSuggestion && !collapsed && (
            <span className="anno__quote" title={a.anchor_quote}>
              “{a.anchor_quote.slice(0, 40)}”
            </span>
          )}
          {collapsed && (
            <span className="anno__preview muted">{truncate(cardPreview(a), 80)}</span>
          )}
          {collapsed && replyCount > 0 && (
            <span
              className="anno__reply-count muted"
              title={t('annotations.repliesHidden', {
                defaultValue: 'Replies hidden in the collapsed thread',
              })}
            >
              ↳ {replyCount}
            </span>
          )}
        </div>

        {isSuggestion && !collapsed && (
          <div className="anno__diff">
            {a.original_text && <del className="anno-del">{a.original_text}</del>}{' '}
            {a.proposed_text && <ins className="anno-ins">{a.proposed_text}</ins>}
          </div>
        )}

        {editId === a.id ? (
          <div className="anno__edit">
            <RichEditor
              value={editBody}
              onChange={setEditBody}
              imageUploadParent={imageUploadParent}
            />
            {/* Sticky sibling of .anno__actions: on a long edit the Save /
                Cancel pair stays reachable just like the view-mode bar. */}
            <div className="anno__edit-actions">
              <button type="button" className="btn--sm" onClick={() => void saveEdit(a)}>
                {t('common.save', { defaultValue: 'Save' })}
              </button>
              <button
                type="button"
                className="btn--sm btn--ghost"
                onClick={() => setEditId(null)}
              >
                {t('common.cancel', { defaultValue: 'Cancel' })}
              </button>
            </div>
          </div>
        ) : (
          !collapsed &&
          a.body && (
            <div className="anno__body">
              <MarkdownView text={a.body} parent={imageUploadParent} />
            </div>
          )
        )}

        {conflictDraft?.id === a.id && editId !== a.id && (
          <div className="anno__conflict" role="status">
            <span>
              {t('annotations.draftKept', {
                defaultValue: 'You have an unsaved draft from before this comment changed.',
              })}
            </span>
            <button
              type="button"
              className="btn--sm"
              onClick={() => {
                setEditId(a.id)
                setEditBody(conflictDraft.body)
                setConflictDraft(null)
                setErr('')
              }}
            >
              {t('annotations.reapplyDraft', { defaultValue: 'Re-apply my draft' })}
            </button>
            <button
              type="button"
              className="btn--sm btn--ghost"
              onClick={() => setConflictDraft(null)}
            >
              {t('annotations.discardDraft', { defaultValue: 'Discard draft' })}
            </button>
          </div>
        )}

        {!isReply && !collapsed && (
          <ul className="anno__replies">
            {repliesOf(a.id).map((r) => renderCard(r, true))}
          </ul>
        )}

        {/* Last content child of the card ON PURPOSE: a bottom-sticky bar
            only pins while its natural position is below the scrollport, so
            anything after it (a long reply thread) would scroll it away.
            Hidden while THIS card is being edited — the sticky
            .anno__edit-actions (Save/Cancel) takes the same bottom strip,
            and a second bar there would paint over it. */}
        {!editing && (
        <div className="anno__actions">
          {/* In-card navigation, useful when a long comment scrolls under
              the (sticky) bar: back to the top of this card / down to its
              end. Hidden on a collapsed card (it is one line tall). */}
          {!collapsed && (
            <>
              <button
                type="button"
                className="btn--sm btn--ghost anno__cardnav"
                title={t('annotations.goToCardStart', {
                  defaultValue: 'Go to the start of this comment',
                })}
                aria-label={t('annotations.goToCardStart', {
                  defaultValue: 'Go to the start of this comment',
                })}
                onClick={() => jumpCard(a.id, 'start')}
              >
                ⤒
              </button>
              <button
                type="button"
                className="btn--sm btn--ghost anno__cardnav"
                title={t('annotations.goToCardEnd', {
                  defaultValue: 'Go to the end of this comment',
                })}
                aria-label={t('annotations.goToCardEnd', {
                  defaultValue: 'Go to the end of this comment',
                })}
                onClick={() => jumpCard(a.id, 'end')}
              >
                ⤓
              </button>
            </>
          )}
          {onJumpToAnchor &&
            (isSuggestion
              ? // An accepted suggestion's original text was spliced away,
                // so there is nothing left to jump to; a rejected one (or
                // a still-open one) keeps it.
                a.original_text && a.status !== 'accepted'
              : a.anchor_quote) && (
            <button
              type="button"
              className="btn--sm btn--ghost anno__locate"
              title={t('annotations.goToAnchor', {
                defaultValue: 'Go to the highlighted text',
              })}
              aria-label={t('annotations.goToAnchor', {
                defaultValue: 'Go to the highlighted text',
              })}
              onClick={() => jumpTo(a)}
            >
              ⌖
            </button>
          )}
          {jumpMiss === a.id && (
            <span className="anno__locate-miss" role="status">
              {t('annotations.anchorNotFound', {
                defaultValue: 'Text not found in the document',
              })}
            </span>
          )}
          {isSuggestion &&
            open &&
            (staleIds.has(a.id) ? (
              // Accept already came back stale for this card: drop the button
              // that would just 400 again, explain why, and leave Reject + the
              // ⌖ "go to text" above so the user can apply it by hand.
              <>
                <span className="anno__stale" role="status">
                  {t('annotations.staleHint', {
                    defaultValue: 'Target text changed — apply by hand',
                  })}
                </span>
                {actBtn(a, 'reject', t('annotations.reject', { defaultValue: 'Reject' }), true)}
              </>
            ) : (
              <>
                {actBtn(a, 'accept', t('annotations.accept', { defaultValue: 'Accept' }))}
                {actBtn(a, 'reject', t('annotations.reject', { defaultValue: 'Reject' }), true)}
              </>
            ))}
          {!isSuggestion && open &&
            actBtn(a, 'resolve', t('annotations.resolve', { defaultValue: 'Resolve' }), true)}
          {!isSuggestion && !open &&
            actBtn(a, 'reopen', t('annotations.reopen', { defaultValue: 'Reopen' }), true)}
          {!isReply && !isSuggestion && (
            <button
              type="button"
              className="btn--sm btn--ghost"
              onClick={() => {
                setReplyTo(replyTo === a.id ? null : a.id)
                setReplyBody('')
              }}
            >
              {t('annotations.reply', { defaultValue: 'Reply' })}
            </button>
          )}
          <button
            type="button"
            className="btn--sm btn--ghost"
            onClick={() => {
              setEditId(a.id)
              setEditBody(a.body ?? '')
            }}
          >
            {t('common.edit', { defaultValue: 'Edit' })}
          </button>
          <button
            type="button"
            className="btn--sm btn--danger"
            onClick={() => void remove(a)}
          >
            ×
          </button>
        </div>
        )}

        {/* NOT gated on ``collapsed``: Reply sits on the always-visible
            action bar, so the composer must open on a folded card too. */}
        {replyTo === a.id && (
          <div className="anno__reply-box">
            <RichEditor
              value={replyBody}
              onChange={setReplyBody}
              imageUploadParent={imageUploadParent}
              placeholder={t('annotations.replyPlaceholder', { defaultValue: 'Reply…' })}
            />
            <button type="button" className="btn--sm" onClick={() => void sendReply(a)}>
              {t('annotations.send', { defaultValue: 'Send' })}
            </button>
          </div>
        )}
      </li>
    )
  }

  // Every visible root folded → the header button flips to "Expand all"
  // (mirrors the note-parts header; a partial state offers "Collapse all").
  const allCollapsed = roots.length > 0 && roots.every((r) => r.ui_collapsed)

  return (
    <section className="anno-panel">
      <header className="anno-panel__head">
        <strong>{title ?? t('annotations.title', { defaultValue: 'Comments & suggestions' })}</strong>
        {roots.length > 1 && (
          <button
            type="button"
            className="btn--sm btn--ghost"
            disabled={uiBusy !== null}
            aria-expanded={!allCollapsed}
            onClick={() => void setAllCollapsed(!allCollapsed)}
            title={
              allCollapsed
                ? t('annotations.expandAllHint', {
                    defaultValue: 'Expand all comments',
                  })
                : t('annotations.collapseAllHint', {
                    defaultValue: 'Collapse all comments',
                  })
            }
          >
            {allCollapsed
              ? `▸ ${t('annotations.expandAll', { defaultValue: 'Expand all' })}`
              : `▾ ${t('annotations.collapseAll', { defaultValue: 'Collapse all' })}`}
          </button>
        )}
        <span className="anno-panel__spacer" />
        <label className="anno-panel__filter">
          <input
            type="checkbox"
            checked={includeResolved}
            onChange={(e) => setIncludeResolved(e.target.checked)}
          />{' '}
          {t('annotations.showResolved', { defaultValue: 'Show resolved' })}
        </label>
      </header>

      {(err || loadError) && <p className="error">{err || loadError}</p>}
      {roots.length === 0 && (
        <p className="hint">
          {t('annotations.empty', { defaultValue: 'No comments yet.' })}
        </p>
      )}

      <ul className="anno-panel__list">{roots.map((a) => renderCard(a, false))}</ul>

      <div className="anno-panel__composer">
        <RichEditor
          value={body}
          onChange={setBody}
          imageUploadParent={imageUploadParent}
          placeholder={t('annotations.commentPlaceholder', {
            defaultValue: 'Add a comment…',
          })}
        />
        <input
          type="text"
          value={quote}
          onChange={(e) => setQuote(e.target.value)}
          placeholder={t('annotations.anchorPlaceholder', {
            defaultValue: 'Anchor to a passage (optional)',
          })}
        />
        <div className="anno-panel__composer-actions">
          <button type="button" className="btn--sm" onClick={() => void addComment()}>
            {t('annotations.comment', { defaultValue: 'Comment' })}
          </button>
          {allowSuggest && (
            <button
              type="button"
              className="btn--sm btn--ghost"
              onClick={() => setShowSuggest((v) => !v)}
            >
              {t('annotations.suggestToggle', { defaultValue: 'Suggest an edit' })}
            </button>
          )}
        </div>
        {allowSuggest && showSuggest && (
          <div className="anno-panel__suggest">
            <input
              type="text"
              value={sugOrig}
              onChange={(e) => setSugOrig(e.target.value)}
              placeholder={t('annotations.original', { defaultValue: 'Original text' })}
            />
            <input
              type="text"
              value={sugProp}
              onChange={(e) => setSugProp(e.target.value)}
              placeholder={t('annotations.proposed', {
                defaultValue: 'Proposed replacement (empty = delete)',
              })}
            />
            <input
              type="text"
              value={sugWhy}
              onChange={(e) => setSugWhy(e.target.value)}
              placeholder={t('annotations.why', { defaultValue: 'Why (optional)' })}
            />
            <button type="button" className="btn--sm" onClick={() => void addSuggestion()}>
              {t('annotations.propose', { defaultValue: 'Propose' })}
            </button>
          </div>
        )}
      </div>
    </section>
  )
}
