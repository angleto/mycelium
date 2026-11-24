import { useCallback, useEffect, useState } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'

import { api, errMessage, workspaceHeader } from '../api/client'
import type { components } from '../shared'

// Distillation-candidate surfacing (task 4995a32f). "Suggest, don't
// automate": memory is a searchable graph and distillation is its
// MAINTENANCE, so this view shows two families of suggested work plus the
// autonomous-atom review inbox.
//
//  - Nodi da distillare : distill | pattern | season candidates (compact
//    inert material into a denser atom).
//  - Archi da curare    : link_add (a strong tag/co-activity pair with no
//    manual link) | link_prune (a `related` link whose basis has decayed)
//    | link_direct (one-way search traversal -> promote to hypha_of).
//  - In attesa di revisione : generated notes awaiting a person's decision
//    (ADR-0043). Each opens the reading view (/garden/review/:id, ADR-0075),
//    where the whole text sits beside its sources and the decision is
//    taken: deciding from a 280-character preview is what that view ended.
//  - Rifiutate : generated notes a person declined (ADR-0075). Kept, out of
//    memory and out of reach of the trash; reopened from here or from the
//    reading view, where they can also be approved or removed for good.
//  - Fatti letti dalle note  : knowledge-graph facts a model extracted on
//    its own, awaiting the same decision (ADR-0071). The first page only;
//    the inbox is newest first and the rest arrives as these are decided.
//
// Actions are wired only where a REST endpoint exists today: single-note
// distill (POST /notes/{id}/distill), reopening a declined note here, and the
// decisions on the reading view. Pattern,
// season and edge curation have no REST endpoint yet — those run via the
// MCP/agent path — so they render read-only with deep-links to the notes,
// never a dead button.

type CandidateNode = components['schemas']['GardenCandidateNode']
type CandidateEdge = components['schemas']['GardenCandidateEdge']
type ReviewItem = components['schemas']['GardenReviewPendingItem']
type RejectedItem = components['schemas']['GardenReviewRejectedItem']
type ProposedFact = components['schemas']['KgProposedFact']

const NODE_GLYPH: Record<string, string> = {
  distill: '⚗️',
  pattern: '🧩',
  season: '🍂',
}

export function GardenReviewRoute() {
  const { t, i18n } = useTranslation()
  const navigate = useNavigate()
  // The reading view sends the person back here after removing a declined
  // note for good, naming it, so the removal is visible where it is missed.
  const removed = (useLocation().state as { removed?: string } | null)?.removed
  const [nodes, setNodes] = useState<CandidateNode[]>([])
  const [edges, setEdges] = useState<CandidateEdge[]>([])
  const [pending, setPending] = useState<ReviewItem[]>([])
  const [rejected, setRejected] = useState<RejectedItem[]>([])
  const [facts, setFacts] = useState<ProposedFact[]>([])
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState<string | null>(null)

  const reload = useCallback(async () => {
    setErr('')
    const [cand, rev, bin, kg] = await Promise.all([
      api.GET('/garden/candidates', {
        params: { header: workspaceHeader(), query: { kind: 'all', limit: 50 } },
      }),
      api.GET('/garden/review/pending', {
        params: { header: workspaceHeader(), query: { limit: 50 } },
      }),
      api.GET('/garden/review/rejected', {
        params: { header: workspaceHeader(), query: { limit: 50 } },
      }),
      api.GET('/kg/review/pending', {
        params: { header: workspaceHeader(), query: { limit: 50 } },
      }),
    ])
    if (cand.error) {
      setErr(errMessage(cand.error))
    } else {
      setNodes(cand.data?.nodes ?? [])
      setEdges(cand.data?.edges ?? [])
    }
    if (rev.error) setErr((e) => e || errMessage(rev.error))
    else setPending(rev.data ?? [])
    if (bin.error) setErr((e) => e || errMessage(bin.error))
    else setRejected(bin.data ?? [])
    if (kg.error) setErr((e) => e || errMessage(kg.error))
    else setFacts(kg.data?.items ?? [])
  }, [])

  useEffect(() => {
    let active = true
    void (async () => {
      await reload()
      if (active) setLoading(false)
    })()
    return () => {
      active = false
    }
  }, [reload])

  const onDistill = useCallback(
    async (noteId: string) => {
      setBusy(`distill:${noteId}`)
      setErr('')
      const { data, error } = await api.POST('/notes/{note_id}/distill', {
        params: { header: workspaceHeader(), path: { note_id: noteId } },
      })
      setBusy(null)
      if (error || !data) {
        setErr(errMessage(error))
        return
      }
      // The distillation is what the person asked to see: open it.
      navigate(`/garden/review/${data.distilled_note_id}`)
    },
    [navigate],
  )

  // Taking a decline back: the note is pending again and back in the inbox.
  const onReopen = useCallback(
    async (noteId: string, expectedVersion: number) => {
      setBusy(`reopen:${noteId}`)
      setErr('')
      const { error } = await api.POST('/garden/review/reopen', {
        params: { header: workspaceHeader() },
        body: { note_id: noteId, expected_version: expectedVersion },
      })
      setBusy(null)
      // Reload FIRST, then report: reload() clears the error banner as its
      // first statement, so setting the message before it would swallow it.
      await reload()
      if (error) setErr(errMessage(error))
    },
    [reload],
  )

  const onFactReview = useCallback(
    async (edgeId: string, action: 'approve' | 'reject', expectedVersion: number) => {
      setBusy(`fact-${action}:${edgeId}`)
      setErr('')
      const path = action === 'approve' ? '/kg/review/approve' : '/kg/review/reject'
      // expected_version = the version this row rendered, as for notes.
      const { error } = await api.POST(path, {
        params: { header: workspaceHeader() },
        body: { edge_id: edgeId, expected_version: expectedVersion },
      })
      setBusy(null)
      if (error) setErr(errMessage(error))
      await reload()
    },
    [reload],
  )

  const total = nodes.length + edges.length + pending.length + rejected.length + facts.length

  return (
    <div className="ghreview">
      <header className="ghreview__head">
        <h1>{t('gardenReview.title')}</h1>
        <Link to="/garden" className="ghreview__back">
          {t('gardenReview.back')}
        </Link>
      </header>
      <p className="ghreview__intro">{t('gardenReview.intro')}</p>

      {removed !== undefined && (
        <p className="hint" role="status">
          {t('gardenReview.removed', { title: removed || t('gardenProposal.untitled') })}
        </p>
      )}
      {err && <p className="error">{err}</p>}
      {loading ? (
        <p className="hint">{t('common.loading')}</p>
      ) : total === 0 ? (
        <p className="ghreview__empty">{t('gardenReview.empty')}</p>
      ) : (
        <>
          {/* ---- Nodi da distillare -------------------------------------- */}
          <section className="ghreview__section">
            <header className="ghreview__sectionhead">
              <h2>{t('gardenReview.nodes.title')}</h2>
              <span className="muted">({nodes.length})</span>
            </header>
            <p className="hint">{t('gardenReview.nodes.hint')}</p>
            {nodes.length === 0 ? (
              <p className="hint">{t('gardenReview.none')}</p>
            ) : (
              <ul className="ghreview__list">
                {nodes.map((n) => {
                  const key = `${n.kind}:${n.note_ids.join(',')}`
                  const canDistill = n.kind === 'distill' && n.note_ids.length === 1
                  return (
                    <li key={key} className="ghreview__item">
                      <span className="ghreview__glyph" aria-hidden="true">
                        {NODE_GLYPH[n.kind] ?? '•'}
                      </span>
                      <span className="ghreview__body">
                        <span className="ghreview__title">{n.title}</span>
                        <span className="ghreview__reason">{n.reason}</span>
                      </span>
                      <span className="chip">{t(`gardenReview.kind.${n.kind}`)}</span>
                      {canDistill ? (
                        <button
                          type="button"
                          className="btn--ghost btn--sm"
                          disabled={busy === `distill:${n.note_ids[0]}`}
                          title={t('gardenReview.distill')}
                          onClick={() => void onDistill(n.note_ids[0])}
                        >
                          ⚗️ {t('gardenReview.distill')}
                        </button>
                      ) : (
                        <span
                          className="muted"
                          title={t('gardenReview.viaAgentHint')}
                        >
                          {t('gardenReview.viaAgent')}
                        </span>
                      )}
                      {n.note_ids.slice(0, 4).map((id) => (
                        <Link key={id} to={`/notes/${id}`} className="btn--ghost btn--sm">
                          {id.slice(0, 8)}…
                        </Link>
                      ))}
                    </li>
                  )
                })}
              </ul>
            )}
          </section>

          {/* ---- Archi da curare ----------------------------------------- */}
          <section className="ghreview__section">
            <header className="ghreview__sectionhead">
              <h2>{t('gardenReview.edges.title')}</h2>
              <span className="muted">({edges.length})</span>
            </header>
            <p className="hint">{t('gardenReview.edges.hint')}</p>
            {edges.length === 0 ? (
              <p className="hint">{t('gardenReview.none')}</p>
            ) : (
              <ul className="ghreview__list">
                {edges.map((e) => {
                  const key = `${e.op}:${e.src_note_id}:${e.dst_note_id}`
                  return (
                    <li key={key} className="ghreview__item">
                      <span className="ghreview__glyph" aria-hidden="true">
                        {e.op === 'add' ? '➕' : e.op === 'direct' ? '⤴️' : '✂️'}
                      </span>
                      <span className="ghreview__body">
                        <span className="ghreview__title">
                          <Link to={`/notes/${e.src_note_id}`}>
                            {e.src_title || e.src_note_id.slice(0, 8)}
                          </Link>{' '}
                          <span aria-hidden="true">{e.op === 'direct' ? '→' : '↔'}</span>{' '}
                          <Link to={`/notes/${e.dst_note_id}`}>
                            {e.dst_title || e.dst_note_id.slice(0, 8)}
                          </Link>
                        </span>
                        <span className="ghreview__reason">{e.reason}</span>
                      </span>
                      <span className="chip">
                        {t(
                          e.op === 'add'
                            ? 'gardenReview.edges.add'
                            : e.op === 'direct'
                              ? 'gardenReview.edges.direct'
                              : 'gardenReview.edges.prune',
                        )}
                      </span>
                      <span className="muted" title={t('gardenReview.viaAgentHint')}>
                        {t('gardenReview.viaAgent')}
                      </span>
                    </li>
                  )
                })}
              </ul>
            )}
          </section>

          {/* ---- In attesa di revisione ---------------------------------- */}
          <section className="ghreview__section">
            <header className="ghreview__sectionhead">
              <h2>{t('gardenReview.pending.title')}</h2>
              <span className="muted">({pending.length})</span>
            </header>
            <p className="hint">{t('gardenReview.pending.hint')}</p>
            {pending.length === 0 ? (
              <p className="hint">{t('gardenReview.none')}</p>
            ) : (
              <ul className="ghreview__list">
                {pending.map((r) => (
                  <li key={r.note_id} className="ghreview__item">
                    <span className="ghreview__glyph" aria-hidden="true">
                      🍄
                    </span>
                    <span className="ghreview__body">
                      <span className="ghreview__title">
                        <Link to={`/garden/review/${r.note_id}`}>
                          {r.title || r.note_id.slice(0, 8)}
                        </Link>
                      </span>
                      <span className="ghreview__reason">{r.preview}</span>
                    </span>
                    {r.humus_kind && <span className="chip">{r.humus_kind}</span>}
                    {r.origin_model_id && (
                      <span className="muted" title={t('gardenReview.pending.model')}>
                        {r.origin_model_id}
                      </span>
                    )}
                    <Link to={`/garden/review/${r.note_id}`} className="btn btn--sm">
                      {t('gardenReview.readAndDecide')}
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </section>

          {/* ---- Fatti letti dalle note ---------------------------------- */}
          <section className="ghreview__section">
            <header className="ghreview__sectionhead">
              <h2>{t('gardenReview.facts.title')}</h2>
              <span className="muted">({facts.length})</span>
            </header>
            <p className="hint">{t('gardenReview.facts.hint')}</p>
            {facts.length === 0 ? (
              <p className="hint">{t('gardenReview.none')}</p>
            ) : (
              <ul className="ghreview__list">
                {facts.map((f) => (
                  <li key={f.edge_id} className="ghreview__item">
                    <span className="ghreview__glyph" aria-hidden="true">
                      🕸️
                    </span>
                    <span className="ghreview__body">
                      <span className="ghreview__title">
                        {f.subject.name} <span className="chip">{f.predicate}</span>{' '}
                        {f.object.name}
                      </span>
                      {f.source_note_id && (
                        <span className="ghreview__reason">
                          {t('gardenReview.facts.source')}{' '}
                          <Link to={`/notes/${f.source_note_id}`}>
                            {f.source_note_title || f.source_note_id.slice(0, 8)}
                          </Link>
                        </span>
                      )}
                    </span>
                    {f.origin_model_id && (
                      <span className="muted" title={t('gardenReview.pending.model')}>
                        {f.origin_model_id}
                      </span>
                    )}
                    <button
                      type="button"
                      className="btn--ghost btn--sm"
                      disabled={busy === `fact-approve:${f.edge_id}`}
                      title={t('gardenReview.approve')}
                      onClick={() => void onFactReview(f.edge_id, 'approve', f.version)}
                    >
                      ✓ {t('gardenReview.approve')}
                    </button>
                    <button
                      type="button"
                      className="btn--ghost btn--sm"
                      disabled={busy === `fact-reject:${f.edge_id}`}
                      title={t('gardenReview.reject')}
                      onClick={() => void onFactReview(f.edge_id, 'reject', f.version)}
                    >
                      × {t('gardenReview.reject')}
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </section>

          {/* ---- Rifiutate ----------------------------------------------- */}
          <section className="ghreview__section">
            <header className="ghreview__sectionhead">
              <h2>{t('gardenReview.rejected.title')}</h2>
              <span className="muted">({rejected.length})</span>
            </header>
            <p className="hint">{t('gardenReview.rejected.hint')}</p>
            {rejected.length === 0 ? (
              <p className="hint">{t('gardenReview.none')}</p>
            ) : (
              <ul className="ghreview__list">
                {rejected.map((r) => (
                  <li key={r.note_id} className="ghreview__item">
                    {/* The glyph says what the row is (a generated note), as
                        in the inbox; a bin here read as "this is lost". */}
                    <span className="ghreview__glyph" aria-hidden="true">
                      🍄
                    </span>
                    <span className="ghreview__body">
                      <span className="ghreview__title">
                        <Link to={`/garden/review/${r.note_id}`}>
                          {r.title || r.note_id.slice(0, 8)}
                        </Link>
                      </span>
                      <span className="ghreview__reason">{r.preview}</span>
                    </span>
                    {r.humus_kind && <span className="chip">{r.humus_kind}</span>}
                    {r.origin_model_id && (
                      <span className="muted" title={t('gardenReview.pending.model')}>
                        {r.origin_model_id}
                      </span>
                    )}
                    <span className="muted" title={t('gardenReview.rejected.at')}>
                      {new Date(r.rejected_at).toLocaleString(i18n.language, {
                        month: 'short',
                        day: 'numeric',
                        hour: '2-digit',
                        minute: '2-digit',
                      })}
                    </span>
                    <Link to={`/garden/review/${r.note_id}`} className="btn--ghost btn--sm">
                      {t('gardenReview.read')}
                    </Link>
                    <button
                      type="button"
                      className="btn--ghost btn--sm"
                      disabled={busy === `reopen:${r.note_id}`}
                      title={t('gardenReview.reopenHint')}
                      onClick={() => void onReopen(r.note_id, r.version)}
                    >
                      {t('gardenReview.reopen')}
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </>
      )}
    </div>
  )
}
