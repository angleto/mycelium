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
   * hidden. Returns false when there was nothing to jump to (raw mode, or
   * the passage was edited away) so the panel can show a brief hint. */
  onJumpToAnchor?: (a: Annotation) => boolean
}

function shortId(id: string | null | undefined): string {
  return id ? id.slice(0, 8) : '—'
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
  // away, or the editor is in raw mode): shows a brief hint, auto-clears.
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

  const [body, setBody] = useState('')
  const [quote, setQuote] = useState('')
  const [showSuggest, setShowSuggest] = useState(false)
  const [sugOrig, setSugOrig] = useState('')
  const [sugProp, setSugProp] = useState('')
  const [sugWhy, setSugWhy] = useState('')

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
      method: 'POST' | 'PATCH' | 'DELETE',
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
    const { ok } = await send(`/annotations/${a.id}`, 'PATCH', {
      body: editBody,
      expected_version: a.version,
    })
    if (ok) {
      setEditId(null)
      setEditBody('')
      await reload()
    }
  }

  const remove = async (a: Annotation) => {
    if (
      !confirm(
        t('annotations.confirmDelete', { defaultValue: 'Remove this annotation?' }),
      )
    )
      return
    if ((await send(`/annotations/${a.id}?expected_version=${a.version}`, 'DELETE')).ok)
      await reload()
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
    return (
      <li key={a.id} className={`anno anno--${a.kind} anno--${a.status}`}>
        <div className="anno__head">
          <span className="anno__author" title={a.author_identity_id ?? ''}>
            {shortId(a.author_identity_id)}
          </span>
          <span className={`anno__status anno__status--${a.status}`}>{a.status}</span>
          {a.anchor_quote && !isSuggestion && (
            <span className="anno__quote" title={a.anchor_quote}>
              “{a.anchor_quote.slice(0, 40)}”
            </span>
          )}
        </div>

        {isSuggestion && (
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
        ) : (
          a.body && (
            <div className="anno__body">
              <MarkdownView text={a.body} parent={imageUploadParent} />
            </div>
          )
        )}

        <div className="anno__actions">
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

        {!isReply && (
          <ul className="anno__replies">
            {repliesOf(a.id).map((r) => renderCard(r, true))}
          </ul>
        )}

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

  return (
    <section className="anno-panel">
      <header className="anno-panel__head">
        <strong>{title ?? t('annotations.title', { defaultValue: 'Comments & suggestions' })}</strong>
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
