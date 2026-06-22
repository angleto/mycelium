import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useState,
} from 'react'
import { createPortal } from 'react-dom'
import { useTranslation } from 'react-i18next'
import type { Editor } from '@tiptap/core'

import * as annoApi from '../lib/annotationsApi'
import { MarkdownView } from './Markdown'
import type { ImageUploadParent } from '../lib/imageUpload'
import type { Annotation, DocKind } from '../lib/useAnnotations'

// The Google-Docs-style inline annotation UX, layered over a RichEditor:
//
//  - select text  → a floating toolbar (💬 / ✎) appears right above the
//    selection; clicking opens a compose popover anchored there. No
//    scrolling to a panel at the bottom.
//  - click an inline mark (a struck suggestion or a highlighted comment)
//    → an action popover opens on the spot with Accept / Reject /
//    Resolve / Edit / Reply / Delete.
//
// All popovers carry an explicit Cancel/Close, and Escape dismisses
// them, so a half-written comment or suggestion can always be abandoned.
//
// The toolbar reads the live editor selection (via the editor's own
// state, not a detached DOM read), so the selection can never be
// collapsed by a blur before the handler runs — the defect that made
// the old toolbar buttons do nothing.

interface Sel {
  from: number
  to: number
  text: string
  prefix: string
  suffix: string
  // Viewport coordinates of the selection, for fixed-position popovers.
  left: number
  top: number
  bottom: number
}

interface ActiveAt {
  id: string
}

interface Props {
  editor: Editor
  docKind: DocKind
  docId: string
  rows: Annotation[]
  reload: () => Promise<void>
  /** Called after accepting a suggestion (the body changed server-side)
   * so the host can refresh the prose. */
  onDocMutated?: () => void | Promise<void>
  /** Suggestions only make sense where there is editable prose to splice
   * into; comments are always allowed. */
  allowSuggest?: boolean
  /** Reported whenever the editor has (or loses) a non-empty selection,
   * so the host toolbar can enable/disable its Comment / Suggest
   * buttons (which drive this annotator through the imperative handle). */
  onSelectableChange?: (canAnnotate: boolean) => void
  /** Owning note/task: lets a saved comment body render `![alt](file)`
   * attachment references in the action popover. */
  parent?: ImageUploadParent
}

// Imperative surface the host editor's toolbar drives: the Comment /
// Suggest buttons live in the (always-visible, sticky) RichEditor
// toolbar, not only in a floating bubble, so they call these to open the
// compose popover on the current selection.
export interface InlineAnnotatorHandle {
  openComment: () => void
  openSuggest: () => void
  // Open the action popover (body + replies + actions) for an existing
  // annotation by id. Driven by the toolbar prev/next so a navigation
  // step reveals the comment/suggestion content, not just flashes the
  // anchored passage in the prose.
  openAnnotation: (id: string) => void
}

// Keep a popover within the viewport horizontally.
function clampLeft(left: number, width = 320): number {
  const margin = 8
  const max = window.innerWidth - width - margin
  return Math.max(margin, Math.min(left, max))
}

export const InlineAnnotator = forwardRef<InlineAnnotatorHandle, Props>(
  function InlineAnnotator(
    {
      editor,
      docKind,
      docId,
      rows,
      reload,
      onDocMutated,
      allowSuggest = true,
      onSelectableChange,
      parent,
    }: Props,
    ref,
  ) {
  const { t } = useTranslation()
  const [sel, setSel] = useState<Sel | null>(null)
  const [compose, setCompose] = useState<{ kind: 'comment' | 'suggest'; sel: Sel } | null>(null)
  const [active, setActive] = useState<ActiveAt | null>(null)
  const [err, setErr] = useState('')
  // Verb of the in-flight accept/reject/resolve/reopen (accept also
  // splices the body server-side then refetches, which takes a beat): the
  // footer buttons disable + spin while it runs, so a slow accept can't be
  // fired twice and the click visibly registers.
  const [pending, setPending] = useState<string | null>(null)

  // Compose form fields.
  const [cBody, setCBody] = useState('')
  const [cProposed, setCProposed] = useState('')
  const [cWhy, setCWhy] = useState('')

  // Inline edit / reply within the action popover.
  const [editing, setEditing] = useState(false)
  const [editText, setEditText] = useState('')
  const [replying, setReplying] = useState(false)
  const [replyText, setReplyText] = useState('')

  const closeAll = useCallback(() => {
    setCompose(null)
    setActive(null)
    setSel(null)
    setEditing(false)
    setReplying(false)
    setErr('')
  }, [])

  // Read the current editor selection as text + W3C prefix/suffix +
  // viewport coordinates. Returns null for an empty/whitespace
  // selection.
  const readSelection = useCallback((): Sel | null => {
    const { state, view } = editor
    const { from, to, empty } = state.selection
    if (empty || to <= from) return null
    const doc = state.doc
    const text = doc.textBetween(from, to, ' ')
    if (!text.trim()) return null
    const prefix = doc.textBetween(doc.resolve(from).start(), from, ' ').slice(-24)
    const suffix = doc.textBetween(to, doc.resolve(to).end(), ' ').slice(0, 24)
    try {
      const a = view.coordsAtPos(from)
      const b = view.coordsAtPos(to)
      return {
        from,
        to,
        text,
        prefix,
        suffix,
        left: (a.left + b.left) / 2,
        top: Math.min(a.top, b.top),
        bottom: Math.max(a.bottom, b.bottom),
      }
    } catch {
      return null
    }
  }, [editor])

  // Track the selection so a compose popover can anchor to it and so the
  // host toolbar's Comment / Suggest buttons enable only when there is
  // something to annotate.
  useEffect(() => {
    const update = () => {
      const s = readSelection()
      setSel(s)
      onSelectableChange?.(s != null)
    }
    update()
    editor.on('selectionUpdate', update)
    return () => {
      editor.off('selectionUpdate', update)
      onSelectableChange?.(false)
    }
  }, [editor, readSelection, onSelectableChange])

  // Click an inline mark → open the action popover on it.
  useEffect(() => {
    const dom = editor.view.dom as HTMLElement
    const onClick = (e: MouseEvent) => {
      const target = e.target as HTMLElement | null
      const el = target?.closest('[data-annotation-id]') as HTMLElement | null
      if (!el) return
      const id = el.getAttribute('data-annotation-id')
      if (!id) return
      setCompose(null)
      setSel(null)
      setEditing(false)
      setReplying(false)
      setErr('')
      setActive({ id })
    }
    dom.addEventListener('click', onClick)
    return () => dom.removeEventListener('click', onClick)
  }, [editor])

  // Escape closes whatever is open.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && (compose || active)) {
        e.preventDefault()
        closeAll()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [compose, active, closeAll])

  const openCompose = useCallback(
    (kind: 'comment' | 'suggest') => {
      // Re-read the live selection rather than trust the tracked ``sel``:
      // when the trigger is a toolbar button, no fresh selectionUpdate
      // fired, but ProseMirror keeps state.selection across the blur.
      const s = readSelection() ?? sel
      if (!s) return
      setCBody('')
      setCProposed('')
      setCWhy('')
      setErr('')
      setActive(null)
      setCompose({ kind, sel: s })
    },
    [readSelection, sel],
  )

  // The host toolbar's Comment / Suggest buttons drive the same compose
  // flow as the (removed) floating bubble used to.
  useImperativeHandle(
    ref,
    () => ({
      openComment: () => openCompose('comment'),
      openSuggest: () => {
        if (allowSuggest) openCompose('suggest')
      },
      openAnnotation: (id: string) => {
        setCompose(null)
        setSel(null)
        setEditing(false)
        setReplying(false)
        setErr('')
        setActive({ id })
      },
    }),
    [openCompose, allowSuggest],
  )

  const submitCompose = async () => {
    if (!compose) return
    const s = compose.sel
    const r =
      compose.kind === 'comment'
        ? await annoApi.createComment({
            docKind,
            docId,
            body: cBody,
            anchorQuote: s.text,
            anchorPrefix: s.prefix,
            anchorSuffix: s.suffix,
          })
        : await annoApi.createSuggestion({
            docKind,
            docId,
            originalText: s.text,
            proposedText: cProposed,
            rationale: cWhy,
            anchorPrefix: s.prefix,
            anchorSuffix: s.suffix,
          })
    if (!r.ok) {
      setErr(r.error ?? 'Error')
      return
    }
    closeAll()
    await reload()
  }

  const current = active ? rows.find((r) => r.id === active.id) ?? null : null

  const doAct = async (a: Annotation, verb: string) => {
    if (pending) return
    setPending(verb)
    try {
      const r = await annoApi.act(a.id, verb, a.version)
      if (!r.ok) {
        setErr(r.error ?? 'Error')
        return
      }
      setErr('')
      await reload()
      if (verb === 'accept') await onDocMutated?.()
      if (verb === 'accept' || verb === 'reject') setActive(null)
    } finally {
      setPending(null)
    }
  }

  // Accept/reject/resolve/reopen button with an in-flight spinner. All
  // such buttons disable while one runs; the clicked one shows the spinner.
  const actBtn = (a: Annotation, verb: string, label: string, ghost = false) => {
    const busy = pending === verb
    return (
      <button
        type="button"
        className={`btn--sm${ghost ? ' btn--ghost' : ''}`}
        disabled={pending !== null}
        aria-busy={busy}
        onClick={() => void doAct(a, verb)}
      >
        {busy && <span className="btn-spinner" aria-hidden="true" />}
        {label}
      </button>
    )
  }

  const doDelete = async (a: Annotation) => {
    if (!confirm(t('annotations.confirmDelete', { defaultValue: 'Remove this annotation?' })))
      return
    const r = await annoApi.remove(a.id, a.version)
    if (!r.ok) {
      setErr(r.error ?? 'Error')
      return
    }
    closeAll()
    await reload()
  }

  const doSaveEdit = async (a: Annotation) => {
    if (!editText.trim()) return
    const r = await annoApi.editBody(a.id, editText, a.version)
    if (!r.ok) {
      setErr(r.error ?? 'Error')
      return
    }
    setEditing(false)
    await reload()
  }

  const doReply = async (a: Annotation) => {
    if (!replyText.trim()) return
    const r = await annoApi.createComment({
      docKind,
      docId,
      body: replyText,
      parentId: a.id,
    })
    if (!r.ok) {
      setErr(r.error ?? 'Error')
      return
    }
    setReplying(false)
    setReplyText('')
    await reload()
  }

  const repliesOf = (id: string) => rows.filter((r) => r.parent_id === id && !r.deleted_at)

  // Render the floating overlay through a portal on <body> so the
  // fixed-position coordinates (computed from the editor selection in
  // viewport space) are immune to a transformed / overflow-clipped
  // ancestor — e.g. the note modal, which otherwise traps position:fixed
  // and clips the bubble out of view.
  return createPortal(
    <>
      {/* The Comment / Suggest triggers now live in the host RichEditor's
          sticky toolbar (driven via the imperative handle above), so the
          old floating selection bubble is gone — the toolbar is always in
          reach and the bubble no longer competes with it. */}

      {/* Compose popover */}
      {compose && (
        <div
          className="anno-pop"
          style={{
            position: 'fixed',
            left: clampLeft(compose.sel.left - 160),
            top: compose.sel.bottom + 8,
          }}
          onMouseDown={(e) => e.stopPropagation()}
        >
          <div className="anno-pop__quote" title={compose.sel.text}>
            “{compose.sel.text.slice(0, 80)}”
          </div>
          {compose.kind === 'comment' ? (
            <textarea
              className="anno-pop__input"
              autoFocus
              rows={3}
              value={cBody}
              onChange={(e) => setCBody(e.target.value)}
              placeholder={t('annotations.commentPlaceholder', { defaultValue: 'Add a comment…' })}
            />
          ) : (
            <>
              <textarea
                className="anno-pop__input"
                autoFocus
                rows={2}
                value={cProposed}
                onChange={(e) => setCProposed(e.target.value)}
                placeholder={t('annotations.proposed', {
                  defaultValue: 'Proposed replacement (empty = delete)',
                })}
              />
              <input
                className="anno-pop__input"
                type="text"
                value={cWhy}
                onChange={(e) => setCWhy(e.target.value)}
                placeholder={t('annotations.why', { defaultValue: 'Why (optional)' })}
              />
            </>
          )}
          {err && <p className="err anno-pop__err">{err}</p>}
          <div className="anno-pop__actions">
            <button type="button" className="btn--sm" onClick={() => void submitCompose()}>
              {compose.kind === 'comment'
                ? t('annotations.comment', { defaultValue: 'Comment' })
                : t('annotations.propose', { defaultValue: 'Propose' })}
            </button>
            <button type="button" className="btn--sm btn--ghost" onClick={closeAll}>
              {t('common.cancel', { defaultValue: 'Cancel' })}
            </button>
          </div>
        </div>
      )}

      {/* Action popover on an existing annotation. Rendered as a centered
          modal (not anchored to the clicked mark) so a tall comment — long
          body + replies + a full row of actions — can never push its
          buttons below the viewport. A click on the backdrop, like Escape,
          closes it; the dialog stops the mousedown so an in-dialog click is
          never read as an outside click. The variable-height content lives
          in a scroll region and the action buttons sit in a pinned footer,
          so the buttons stay in view however long the comment runs. */}
      {active && current && (
        <div className="anno-modal-backdrop" onMouseDown={closeAll}>
          <div
            className="anno-pop anno-pop--modal"
            role="dialog"
            aria-modal="true"
            onMouseDown={(e) => e.stopPropagation()}
          >
            <div className="anno-pop__scroll">
              {current.kind === 'suggestion' ? (
                <div className="anno-pop__diff">
                  {current.original_text && <del className="anno-del">{current.original_text}</del>}{' '}
                  {current.proposed_text && <ins className="anno-ins">{current.proposed_text}</ins>}
                </div>
              ) : (
                current.anchor_quote && (
                  <div className="anno-pop__quote" title={current.anchor_quote}>
                    “{current.anchor_quote.slice(0, 80)}”
                  </div>
                )
              )}

              {editing ? (
                <textarea
                  className="anno-pop__input"
                  autoFocus
                  rows={3}
                  value={editText}
                  onChange={(e) => setEditText(e.target.value)}
                />
              ) : (
                current.body && (
                  <div className="anno-pop__body">
                    <MarkdownView text={current.body} parent={parent} />
                  </div>
                )
              )}

              {/* Existing replies (read-only here; full thread in the panel). */}
              {repliesOf(current.id).length > 0 && (
                <ul className="anno-pop__replies">
                  {repliesOf(current.id).map((r) => (
                    <li key={r.id}>{r.body}</li>
                  ))}
                </ul>
              )}

              {replying && (
                <textarea
                  className="anno-pop__input"
                  autoFocus
                  rows={2}
                  value={replyText}
                  onChange={(e) => setReplyText(e.target.value)}
                  placeholder={t('annotations.replyPlaceholder', { defaultValue: 'Reply…' })}
                />
              )}
            </div>

            {err && <p className="err anno-pop__err">{err}</p>}

            {/* Pinned footer: the mode-appropriate action buttons, kept below
                the scroll region so they never spill off-screen. */}
            {editing ? (
              <div className="anno-pop__actions anno-pop__footer">
                <button type="button" className="btn--sm" onClick={() => void doSaveEdit(current)}>
                  {t('common.save', { defaultValue: 'Save' })}
                </button>
                <button
                  type="button"
                  className="btn--sm btn--ghost"
                  onClick={() => setEditing(false)}
                >
                  {t('common.cancel', { defaultValue: 'Cancel' })}
                </button>
              </div>
            ) : replying ? (
              <div className="anno-pop__actions anno-pop__footer">
                <button type="button" className="btn--sm" onClick={() => void doReply(current)}>
                  {t('annotations.send', { defaultValue: 'Send' })}
                </button>
                <button
                  type="button"
                  className="btn--sm btn--ghost"
                  onClick={() => setReplying(false)}
                >
                  {t('common.cancel', { defaultValue: 'Cancel' })}
                </button>
              </div>
            ) : (
              <div className="anno-pop__actions anno-pop__footer">
                {current.kind === 'suggestion' && current.status === 'open' && (
                  <>
                    {actBtn(current, 'accept', t('annotations.accept', { defaultValue: 'Accept' }))}
                    {actBtn(current, 'reject', t('annotations.reject', { defaultValue: 'Reject' }), true)}
                  </>
                )}
                {current.kind === 'comment' && current.status === 'open' &&
                  actBtn(current, 'resolve', t('annotations.resolve', { defaultValue: 'Resolve' }), true)}
                {current.kind === 'comment' && current.status !== 'open' &&
                  actBtn(current, 'reopen', t('annotations.reopen', { defaultValue: 'Reopen' }), true)}
                {current.kind === 'comment' && (
                  <button
                    type="button"
                    className="btn--sm btn--ghost"
                    onClick={() => {
                      setReplyText('')
                      setReplying(true)
                    }}
                  >
                    {t('annotations.reply', { defaultValue: 'Reply' })}
                  </button>
                )}
                <button
                  type="button"
                  className="btn--sm btn--ghost"
                  onClick={() => {
                    setEditText(current.body ?? '')
                    setEditing(true)
                  }}
                >
                  {t('common.edit', { defaultValue: 'Edit' })}
                </button>
                <button
                  type="button"
                  className="btn--sm btn--danger"
                  onClick={() => void doDelete(current)}
                >
                  ×
                </button>
                <button type="button" className="btn--sm btn--ghost" onClick={closeAll}>
                  {t('common.close', { defaultValue: 'Close' })}
                </button>
              </div>
            )}
          </div>
        </div>
      )}
    </>,
    document.body,
  )
})
