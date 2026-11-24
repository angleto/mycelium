import { useCallback, useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'

import { api, errMessage, workspaceHeader } from '../api/client'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { MarkdownView } from '../components/Markdown'
import type { components } from '../shared'

// ADR-0075 decision 2: a proposal is read before it is decided. The review
// gate keeps a generated note out of search, listings and agents until a
// person approves it, and until this view the person deciding could read 280
// characters of it: the inbox linked to /notes/{id}, which answers 404 for a
// proposal by design. This is the one place that serves the whole text, beside
// the whole text of the notes it was drawn from, with the actions its state
// allows, each named by what it does.
//
// The state decides everything on the page, so it is spelled out in words and
// never left to an icon: pending (not in your memory yet), declined (kept here,
// out of your memory, until you reopen, approve or remove it), approved (an
// ordinary note now).

type Reading = components['schemas']['GardenReviewReadingOut']

type Busy = 'approve' | 'decline' | 'reopen' | 'remove' | null

export function GardenProposalRoute() {
  const { noteId = '' } = useParams<{ noteId: string }>()
  const { t, i18n } = useTranslation()
  const navigate = useNavigate()
  const [reading, setReading] = useState<Reading | null>(null)
  const [loading, setLoading] = useState(true)
  const [missing, setMissing] = useState(false)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState<Busy>(null)
  const [confirmRemove, setConfirmRemove] = useState(false)

  const load = useCallback(async () => {
    const { data, error, response } = await api.GET('/garden/review/nodes/{note_id}', {
      params: { header: workspaceHeader(), path: { note_id: noteId } },
    })
    if (error) {
      if (response.status === 404) setMissing(true)
      else setErr(errMessage(error))
      return
    }
    setReading(data)
  }, [noteId])

  useEffect(() => {
    let active = true
    void (async () => {
      await load()
      if (active) setLoading(false)
    })()
    return () => {
      active = false
    }
  }, [load])

  const when = (iso: string | null | undefined) =>
    iso
      ? new Date(iso).toLocaleString(i18n.language, {
          day: 'numeric',
          month: 'short',
          year: 'numeric',
          hour: '2-digit',
          minute: '2-digit',
        })
      : ''

  // Every action echoes the version the page rendered: a proposal that changed
  // after it was read is refused, and the page reloads to show what it is now.
  const act = useCallback(
    async (action: Exclude<Busy, null>) => {
      if (!reading) return
      setBusy(action)
      setErr('')
      const body = { note_id: reading.note_id, expected_version: reading.version }
      const res =
        action === 'approve'
          ? await api.POST('/garden/review/approve', {
              params: { header: workspaceHeader() },
              body: { ...body, reason: null },
            })
          : action === 'decline'
            ? await api.POST('/garden/review/reject', {
                params: { header: workspaceHeader() },
                body: { ...body, reason: null },
              })
            : action === 'reopen'
              ? await api.POST('/garden/review/reopen', {
                  params: { header: workspaceHeader() },
                  body,
                })
              : await api.POST('/garden/review/remove', {
                  params: { header: workspaceHeader() },
                  body,
                })
      setBusy(null)
      if (res.error) {
        await load()
        setErr(errMessage(res.error))
        return
      }
      if (action === 'remove') {
        setConfirmRemove(false)
        navigate('/garden/review', { state: { removed: reading.title ?? '' } })
        return
      }
      await load()
    },
    [reading, load, navigate],
  )

  if (loading) {
    return (
      <section className="card card--wide gproposal">
        <p className="hint">{t('common.loading')}</p>
      </section>
    )
  }
  if (missing || !reading) {
    return (
      <section className="card card--wide gproposal">
        <Link to="/garden/review" className="ghreview__back">
          {t('gardenProposal.back')}
        </Link>
        {err ? <p className="error">{err}</p> : <p className="hint">{t('gardenProposal.missing')}</p>}
      </section>
    )
  }

  const state = reading.review_state
  const kind = reading.humus_kind ?? 'distillation'

  return (
    <section className="card card--wide gproposal" aria-labelledby="gproposal-title">
      <Link to="/garden/review" className="ghreview__back">
        {t('gardenProposal.back')}
      </Link>
      <header className="gproposal__head">
        <h1 id="gproposal-title">{reading.title || t('gardenProposal.untitled')}</h1>
        <p className="gproposal__meta">
          <span className={`chip gproposal__state gproposal__state--${state}`}>
            {t(`gardenProposal.state.${state}`)}
          </span>
          <span>{t(`gardenProposal.kind.${kind}`, { defaultValue: kind })}</span>
          {reading.origin_model_id && (
            <span>{t('gardenProposal.writtenBy', { model: reading.origin_model_id })}</span>
          )}
          <span>{t('gardenProposal.createdAt', { when: when(reading.created_at) })}</span>
        </p>
        <p className="gproposal__explain">
          {t(`gardenProposal.explain.${state}`, { when: when(reading.decided_at) })}
        </p>
      </header>

      {err && <p className="error">{err}</p>}

      <div className="gproposal__actions" role="group" aria-label={t('gardenProposal.actions')}>
        {state === 'proposed' && (
          <>
            <button type="button" disabled={busy !== null} onClick={() => void act('approve')}>
              {t('gardenProposal.approve')}
            </button>
            <button
              type="button"
              className="btn--ghost"
              disabled={busy !== null}
              onClick={() => void act('decline')}
            >
              {t('gardenProposal.decline')}
            </button>
          </>
        )}
        {state === 'declined' && (
          <>
            <button type="button" disabled={busy !== null} onClick={() => void act('approve')}>
              {t('gardenProposal.approve')}
            </button>
            <button
              type="button"
              className="btn--ghost"
              disabled={busy !== null}
              onClick={() => void act('reopen')}
            >
              {t('gardenProposal.reopen')}
            </button>
            <button
              type="button"
              className="btn--ghost gproposal__remove"
              disabled={busy !== null}
              onClick={() => setConfirmRemove(true)}
            >
              {t('gardenProposal.remove')}
            </button>
          </>
        )}
        {state === 'approved' && (
          <Link to={`/notes/${reading.note_id}`} className="btn">
            {t('gardenProposal.openNote')}
          </Link>
        )}
      </div>
      <p className="hint gproposal__actionshint">{t(`gardenProposal.actionsHint.${state}`)}</p>

      <div className="gproposal__columns">
        <article className="gproposal__col" aria-labelledby="gproposal-sources">
          <h2 id="gproposal-sources">
            {t('gardenProposal.sources', { count: reading.sources.length })}
          </h2>
          {reading.sources.length === 0 ? (
            <p className="hint">{t('gardenProposal.noSources')}</p>
          ) : (
            reading.sources.map((src) => (
              <section key={src.note_id} className="gproposal__source">
                <h3>
                  <Link to={`/notes/${src.note_id}`}>{src.title || t('gardenProposal.untitled')}</Link>
                  {src.is_archived && (
                    <span className="chip gproposal__archived">{t('gardenProposal.archived')}</span>
                  )}
                </h3>
                <MarkdownView text={src.body} />
              </section>
            ))
          )}
        </article>
        <article className="gproposal__col gproposal__col--proposal" aria-labelledby="gproposal-text">
          <h2 id="gproposal-text">{t(`gardenProposal.proposalTitle.${state}`)}</h2>
          <MarkdownView text={reading.body} />
        </article>
      </div>

      {confirmRemove && (
        <ConfirmDialog
          title={t('gardenProposal.removeTitle')}
          intro={t('gardenProposal.removeIntro', {
            title: reading.title || t('gardenProposal.untitled'),
          })}
          confirmLabel={t('gardenProposal.removeConfirm')}
          danger
          busy={busy === 'remove'}
          error={err || null}
          onConfirm={() => void act('remove')}
          onClose={() => setConfirmRemove(false)}
        >
          <p className="hint">{t('gardenProposal.removeDetail')}</p>
        </ConfirmDialog>
      )}
    </section>
  )
}
