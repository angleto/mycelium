import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'

import { api, errMessage, workspaceHeader } from '../api/client'
import type { components } from '../api/schema'

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
//  - In attesa di revisione : autonomously-produced humus atoms awaiting a
//    human approve/reject (ADR-0043).
//
// Actions are wired only where a REST endpoint exists today: single-note
// distill (POST /notes/{id}/distill) and review approve/reject. Pattern,
// season and edge curation have no REST endpoint yet — those run via the
// MCP/agent path — so they render read-only with deep-links to the notes,
// never a dead button.

type CandidateNode = components['schemas']['GardenCandidateNode']
type CandidateEdge = components['schemas']['GardenCandidateEdge']
type ReviewItem = components['schemas']['GardenReviewPendingItem']

const NODE_GLYPH: Record<string, string> = {
  distill: '⚗️',
  pattern: '🧩',
  season: '🍂',
}

export function GardenReviewRoute() {
  const { t } = useTranslation()
  const [nodes, setNodes] = useState<CandidateNode[]>([])
  const [edges, setEdges] = useState<CandidateEdge[]>([])
  const [pending, setPending] = useState<ReviewItem[]>([])
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState<string | null>(null)

  const reload = useCallback(async () => {
    setErr('')
    const [cand, rev] = await Promise.all([
      api.GET('/garden/candidates', {
        params: { header: workspaceHeader(), query: { kind: 'all', limit: 50 } },
      }),
      api.GET('/garden/review/pending', {
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
      const { error } = await api.POST('/notes/{note_id}/distill', {
        params: { header: workspaceHeader(), path: { note_id: noteId } },
      })
      setBusy(null)
      if (error) {
        setErr(errMessage(error))
        return
      }
      await reload()
    },
    [reload],
  )

  const onReview = useCallback(
    async (noteId: string, action: 'approve' | 'reject', expectedVersion: number) => {
      setBusy(`${action}:${noteId}`)
      setErr('')
      const path = action === 'approve' ? '/garden/review/approve' : '/garden/review/reject'
      const { error } = await api.POST(path, {
        params: { header: workspaceHeader() },
        // expected_version = the version this card rendered: the server
        // rejects with stale_version if the note changed after we read it
        // (TOCTOU guard, task 2e36e732).
        body: { note_id: noteId, reason: null, expected_version: expectedVersion },
      })
      setBusy(null)
      if (error) {
        setErr(errMessage(error))
        await reload() // refresh the card so the reviewer sees the CURRENT content
        return
      }
      await reload()
    },
    [reload],
  )

  const total = nodes.length + edges.length + pending.length

  return (
    <div className="ghreview">
      <header className="ghreview__head">
        <h1>{t('gardenReview.title')}</h1>
        <Link to="/garden" className="ghreview__back">
          {t('gardenReview.back')}
        </Link>
      </header>
      <p className="ghreview__intro">{t('gardenReview.intro')}</p>

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
                        <Link to={`/notes/${r.note_id}`}>
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
                    <button
                      type="button"
                      className="btn--ghost btn--sm"
                      disabled={busy === `approve:${r.note_id}`}
                      title={t('gardenReview.approve')}
                      onClick={() => void onReview(r.note_id, 'approve', r.version)}
                    >
                      ✓ {t('gardenReview.approve')}
                    </button>
                    <button
                      type="button"
                      className="btn--ghost btn--sm"
                      disabled={busy === `reject:${r.note_id}`}
                      title={t('gardenReview.reject')}
                      onClick={() => void onReview(r.note_id, 'reject', r.version)}
                    >
                      × {t('gardenReview.reject')}
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
