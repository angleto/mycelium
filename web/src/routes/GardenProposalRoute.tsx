import { useCallback, useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'

import { api, errMessage, workspaceHeader } from '../api/client'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { CopyIdButton } from '../components/CopyIdButton'
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
type Check = components['schemas']['GardenReviewCheckOut']
type AgentDecision = components['schemas']['GardenDelegatedDecisionOut']

// ADR-0076: an agent may have checked this proposal against its sources, or
// written a better version of it. Neither decides anything; both are shown
// here, in words, so the person decides with them in view.
function CheckReport({ check, when }: { check: Check; when: (iso: string) => string }) {
  const { t } = useTranslation()
  return (
    <li className="gproposal__check">
      <p className="gproposal__checkhead">
        <span className={`chip gproposal__verdict gproposal__verdict--${check.verdict}`}>
          {t(`gardenProposal.check.verdict.${check.verdict}`)}
        </span>
        <span>
          {t('gardenProposal.check.by', {
            model: check.checker_model_id || t('gardenProposal.check.unknownModel'),
            when: when(check.created_at),
          })}
        </span>
        {check.stale && <span className="chip">{t('gardenProposal.check.stale')}</span>}
      </p>
      <p>{check.summary}</p>
      <ul className="gproposal__findings">
        {check.findings.map((f, i) => (
          <li key={i} className={`gproposal__finding gproposal__finding--${f.status}`}>
            <span className="gproposal__findingstatus">
              {t(`gardenProposal.check.status.${f.status}`)}
            </span>
            <span className="gproposal__claim">{f.claim}</span>
            {f.evidence && <blockquote className="gproposal__evidence">{f.evidence}</blockquote>}
            {f.comment && <span className="hint">{f.comment}</span>}
          </li>
        ))}
      </ul>
    </li>
  )
}

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
  // ADR-0076 phase B: the standing decision an assistant took on this note,
  // if any, shown with the way to take it back.
  const [agentDecision, setAgentDecision] = useState<AgentDecision | null>(null)

  const load = useCallback(async () => {
    // The person reads the sources whole: the budget that keeps an agent's
    // result small is for MCP, not for this page.
    const { data, error, response } = await api.GET('/garden/review/nodes/{note_id}', {
      params: {
        header: workspaceHeader(),
        path: { note_id: noteId },
        query: { full_text: true },
      },
    })
    if (error) {
      if (response.status === 404) setMissing(true)
      else setErr(errMessage(error))
      return
    }
    setReading(data)
    const deleg = await api.GET('/garden/delegation/decisions', {
      params: { header: workspaceHeader(), query: { limit: 200 } },
    })
    setAgentDecision(
      (deleg.data ?? []).find((d) => d.target_id === noteId && !d.reversed_at) ?? null,
    )
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

  async function revertAgent(decisionId: string) {
    setBusy('reopen')
    setErr('')
    const { error } = await api.POST('/garden/delegation/revert', {
      params: { header: workspaceHeader() },
      body: { decision_id: decisionId },
    })
    setBusy(null)
    await load()
    if (error) setErr(errMessage(error))
  }

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
          {/* The code to hand to an assistant ("check 833743fb"): the full
              id is what the button copies. */}
          <CopyIdButton
            id={reading.note_id}
            label={t('gardenProposal.copyId')}
            copiedLabel={t('gardenProposal.idCopied')}
          />
        </p>
        <p className="gproposal__explain">
          {t(`gardenProposal.explain.${state}`, { when: when(reading.decided_at) })}
        </p>
      </header>

      {err && <p className="error">{err}</p>}

      {agentDecision && (
        <div className="banner gproposal__agent" role="note">
          <p>
            {t('gardenProposal.agentDecided', {
              what: t(`gardenDelegation.kind.${agentDecision.kind}.name`),
            })}
            {agentDecision.reason ? ` ${agentDecision.reason}` : ''}
          </p>
          <button
            type="button"
            className="btn--ghost btn--sm"
            disabled={busy !== null}
            onClick={() => void revertAgent(agentDecision.decision_id)}
          >
            {t('gardenReview.agent.revert')}
          </button>
        </div>
      )}

      {reading.revised_by && reading.revised_by.length > 0 && (
        <p className="banner">
          {t('gardenProposal.revisedBy')}{' '}
          {reading.revised_by.map((r) => (
            <Link key={r.note_id} to={`/garden/review/${r.note_id}`}>
              {t('gardenProposal.openRevision', {
                model: r.origin_model_id || t('gardenProposal.check.unknownModel'),
              })}
            </Link>
          ))}
        </p>
      )}
      {reading.revises && (
        <div className="banner gproposal__revises">
          <p>
            {t('gardenProposal.revises', {
              model: reading.revises.origin_model_id || t('gardenProposal.check.unknownModel'),
            })}{' '}
            <Link to={`/garden/review/${reading.revises.note_id}`}>
              {t('gardenProposal.openOriginal')}
            </Link>
          </p>
          <details>
            <summary>{t('gardenProposal.showOriginal')}</summary>
            <MarkdownView text={reading.revises.body ?? ''} />
          </details>
        </div>
      )}

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

      {reading.checks && reading.checks.length > 0 && (
        <section className="gproposal__checks" aria-labelledby="gproposal-checks">
          <h2 id="gproposal-checks">{t('gardenProposal.check.title')}</h2>
          <p className="hint">{t('gardenProposal.check.hint')}</p>
          <ul className="gproposal__checklist">
            {reading.checks.map((c) => (
              <CheckReport key={c.check_id} check={c} when={(iso) => when(iso)} />
            ))}
          </ul>
        </section>
      )}

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
