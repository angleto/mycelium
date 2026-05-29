import { useCallback, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { authFetch, errMessage } from '../api/client'
import type { components } from '../api/schema'

type Annotation = components['schemas']['AnnotationOut']
type DocKind = 'note_part' | 'task_description'

// The inline annotation layer (comments + suggestions) for a single
// markdown document, addressed by the generic (docKind, docId) handle.
// The same data renders on web/CLI/MCP; here a suggestion shows the
// struck original + the proposed replacement (Google-Docs "suggesting"
// mode), accepted/rejected in place. The canonical markdown is never
// touched until a suggestion is accepted.
interface Props {
  docKind: DocKind
  docId: string
  /** Heading; e.g. "Work diary" for a task description. */
  title?: string
  /** Whether the suggestion composer is offered (only where the document
   * has editable prose to splice into). Defaults to true. */
  allowSuggest?: boolean
}

function shortId(id: string | null | undefined): string {
  return id ? id.slice(0, 8) : '—'
}

export function AnnotationsPanel({
  docKind,
  docId,
  title,
  allowSuggest = true,
}: Props) {
  const { t } = useTranslation()
  const [rows, setRows] = useState<Annotation[]>([])
  const [err, setErr] = useState('')
  const [loaded, setLoaded] = useState(false)
  const [includeResolved, setIncludeResolved] = useState(true)

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

  const reload = useCallback(async () => {
    // All setState happen after the first await so the on-mount effect
    // never sets state synchronously (react-hooks/set-state-in-effect).
    // Always fetch the full set; the "show resolved" toggle filters
    // client-side, so reload depends only on the (stable) document
    // handle — no stateful dep that would make the on-mount effect a
    // cascading-render risk (react-hooks/set-state-in-effect).
    const qs = new URLSearchParams({
      doc_kind: docKind,
      doc_id: docId,
      include_resolved: 'true',
    })
    try {
      const res = await authFetch(`/annotations?${qs.toString()}`)
      if (!res.ok) {
        setErr(`HTTP ${res.status}`)
        return
      }
      setRows((await res.json()) as Annotation[])
      setErr('')
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setLoaded(true)
    }
  }, [docKind, docId])

  useEffect(() => {
    // Fetch-on-mount: reload() only setStates after its first await, so
    // there is no synchronous cascade; the lint rule cannot see through
    // the awaited body. Disabled here as the SPA does for data-load
    // effects.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void reload()
  }, [reload])

  const send = useCallback(
    async (
      path: string,
      method: 'POST' | 'PATCH' | 'DELETE',
      payload?: Record<string, unknown>,
    ): Promise<boolean> => {
      const res = await authFetch(path, {
        method,
        headers: payload ? { 'Content-Type': 'application/json' } : undefined,
        body: payload ? JSON.stringify(payload) : undefined,
      })
      if (!res.ok) {
        setErr(errMessage(await res.json().catch(() => ({}))))
        return false
      }
      setErr('')
      return true
    },
    [],
  )

  const addComment = async () => {
    if (!body.trim()) return
    const ok = await send('/annotations/comment', 'POST', {
      doc_kind: docKind,
      doc_id: docId,
      body,
      anchor_quote: quote || null,
    })
    if (ok) {
      setBody('')
      setQuote('')
      await reload()
    }
  }

  const addSuggestion = async () => {
    if (!sugOrig.trim()) return
    const ok = await send('/annotations/suggestion', 'POST', {
      doc_kind: docKind,
      doc_id: docId,
      original_text: sugOrig,
      proposed_text: sugProp,
      rationale: sugWhy,
    })
    if (ok) {
      setSugOrig('')
      setSugProp('')
      setSugWhy('')
      setShowSuggest(false)
      await reload()
    }
  }

  const act = async (a: Annotation, verb: string) => {
    if (await send(`/annotations/${a.id}/${verb}`, 'POST', { expected_version: a.version }))
      await reload()
  }

  const sendReply = async (parent: Annotation) => {
    if (!replyBody.trim()) return
    const ok = await send('/annotations/comment', 'POST', {
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
    const ok = await send(`/annotations/${a.id}`, 'PATCH', {
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
    if (await send(`/annotations/${a.id}?expected_version=${a.version}`, 'DELETE'))
      await reload()
  }

  const roots = rows.filter(
    (r) => !r.parent_id && (includeResolved || r.status === 'open'),
  )
  const repliesOf = (id: string) => rows.filter((r) => r.parent_id === id)

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
            <textarea
              value={editBody}
              onChange={(e) => setEditBody(e.target.value)}
              rows={2}
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
          a.body && <div className="anno__body">{a.body}</div>
        )}

        <div className="anno__actions">
          {isSuggestion && open && (
            <>
              <button type="button" className="btn--sm" onClick={() => void act(a, 'accept')}>
                {t('annotations.accept', { defaultValue: 'Accept' })}
              </button>
              <button
                type="button"
                className="btn--sm btn--ghost"
                onClick={() => void act(a, 'reject')}
              >
                {t('annotations.reject', { defaultValue: 'Reject' })}
              </button>
            </>
          )}
          {!isSuggestion && open && (
            <button
              type="button"
              className="btn--sm btn--ghost"
              onClick={() => void act(a, 'resolve')}
            >
              {t('annotations.resolve', { defaultValue: 'Resolve' })}
            </button>
          )}
          {!isSuggestion && !open && (
            <button
              type="button"
              className="btn--sm btn--ghost"
              onClick={() => void act(a, 'reopen')}
            >
              {t('annotations.reopen', { defaultValue: 'Reopen' })}
            </button>
          )}
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
            <textarea
              value={replyBody}
              onChange={(e) => setReplyBody(e.target.value)}
              rows={2}
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

      {err && <p className="error">{err}</p>}
      {!loaded && <p className="hint">{t('common.loading', { defaultValue: 'Loading…' })}</p>}
      {loaded && roots.length === 0 && (
        <p className="hint">
          {t('annotations.empty', { defaultValue: 'No comments yet.' })}
        </p>
      )}

      <ul className="anno-panel__list">{roots.map((a) => renderCard(a, false))}</ul>

      <div className="anno-panel__composer">
        <textarea
          value={body}
          onChange={(e) => setBody(e.target.value)}
          rows={2}
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
