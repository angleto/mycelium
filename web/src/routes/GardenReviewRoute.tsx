import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'

import { api, errMessage, workspaceHeader } from '../api/client'
import { CopyIdButton } from '../components/CopyIdButton'
import type { components } from '../shared'

// The garden's proposals, in one place (ADR-0075).
//
// This page was one long scroll of six sections, each with its own verbs and
// its own idea of what a decision meant: nodes to distil, edges to curate
// (glyph-only rows nobody could act on), generated notes, facts, a bin, and
// the agents' decisions. It is four tabs now, each a short list:
//
//  - To decide: every pending proposal, of every kind, newest first, with a
//    filter by kind. A synthesis is decided on its reading view, after reading
//    it whole beside its sources; a link and a fact are decided on the row.
//  - Decided: what was decided, newest decision first, with Reopen wherever
//    the decision can be taken back.
//  - Decided by an agent: what assistants decided on the person's behalf
//    (ADR-0076 phase B), each with Undo.
//  - Suggested work: what the garden suggests doing, only where the web can
//    do it (distil, synthesise a pattern or a season, propose a link). The
//    suggestions without an action here are counted, not drawn as controls.
//
// Every control is a word naming what it does, and a decision taken here can
// be undone on the spot.

type Proposal = components['schemas']['ProposalOut']
type Kind = Proposal['kind']
type AgentDecision = components['schemas']['GardenDelegatedDecisionOut']
type CandidateNode = components['schemas']['GardenCandidateNode']
type CandidateEdge = components['schemas']['GardenCandidateEdge']

type Tab = 'pending' | 'decided' | 'agents' | 'work'
const TABS: readonly Tab[] = ['pending', 'decided', 'agents', 'work']
type KindFilter = 'all' | Kind
const KIND_FILTERS: readonly KindFilter[] = ['all', 'synthesis', 'link', 'fact']

/** What the person just decided, kept on its row so it can be undone. */
type Done = { kind: Kind; id: string; verdict: 'accept' | 'decline' }

export function GardenReviewRoute() {
  const { t, i18n } = useTranslation()
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()
  const tab: Tab = (TABS as readonly string[]).includes(params.get('tab') ?? '')
    ? (params.get('tab') as Tab)
    : 'pending'
  const [filter, setFilter] = useState<KindFilter>('all')
  // The reading view sends the person back here after removing a declined
  // note for good, naming it, so the removal is visible where it is missed.
  const removed = (useLocation().state as { removed?: string } | null)?.removed

  const [pending, setPending] = useState<Proposal[]>([])
  const [pendingMore, setPendingMore] = useState<string | null>(null)
  const [decided, setDecided] = useState<Proposal[]>([])
  const [decidedMore, setDecidedMore] = useState<string | null>(null)
  const [agents, setAgents] = useState<AgentDecision[]>([])
  const [nodes, setNodes] = useState<CandidateNode[]>([])
  const [edges, setEdges] = useState<CandidateEdge[]>([])
  const [linkKind, setLinkKind] = useState<Record<string, string>>({})
  const [done, setDone] = useState<Record<string, Done>>({})
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState<string | null>(null)

  const reload = useCallback(async () => {
    setErr('')
    const [p, d, a, c] = await Promise.all([
      api.GET('/garden/proposals', {
        params: { header: workspaceHeader(), query: { listing: 'pending', limit: 50 } },
      }),
      api.GET('/garden/proposals', {
        params: { header: workspaceHeader(), query: { listing: 'decided', limit: 50 } },
      }),
      api.GET('/garden/delegation/decisions', {
        params: { header: workspaceHeader(), query: { limit: 50 } },
      }),
      api.GET('/garden/candidates', {
        params: { header: workspaceHeader(), query: { kind: 'all', limit: 50 } },
      }),
    ])
    const first = p.error ?? d.error ?? a.error ?? c.error
    if (first) setErr(errMessage(first))
    setPending(p.data?.items ?? [])
    setPendingMore(p.data?.next_cursor ?? null)
    setDecided(d.data?.items ?? [])
    setDecidedMore(d.data?.next_cursor ?? null)
    setAgents(a.data ?? [])
    setNodes(c.data?.nodes ?? [])
    setEdges(c.data?.edges ?? [])
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

  const more = useCallback(
    async (listing: 'pending' | 'decided') => {
      const cursor = listing === 'pending' ? pendingMore : decidedMore
      if (!cursor) return
      const { data, error } = await api.GET('/garden/proposals', {
        params: { header: workspaceHeader(), query: { listing, limit: 50, cursor } },
      })
      if (error) {
        setErr(errMessage(error))
        return
      }
      if (listing === 'pending') {
        setPending((xs) => [...xs, ...(data?.items ?? [])])
        setPendingMore(data?.next_cursor ?? null)
      } else {
        setDecided((xs) => [...xs, ...(data?.items ?? [])])
        setDecidedMore(data?.next_cursor ?? null)
      }
    },
    [pendingMore, decidedMore],
  )

  const decide = useCallback(
    async (p: Proposal, verdict: 'accept' | 'decline') => {
      setBusy(`decide:${p.id}`)
      setErr('')
      const { error } = await api.POST('/garden/proposals/decide', {
        params: { header: workspaceHeader() },
        body: {
          kind: p.kind,
          id: p.id,
          verdict,
          expected_version: p.version ?? null,
          link_kind: p.kind === 'link' ? (linkKind[p.id] ?? p.link_kinds?.[0] ?? null) : null,
        },
      })
      setBusy(null)
      if (error) {
        setErr(errMessage(error))
        await reload()
        return
      }
      setDone((m) => ({ ...m, [p.id]: { kind: p.kind, id: p.id, verdict } }))
    },
    [linkKind, reload],
  )

  const reopen = useCallback(
    async (kind: Kind, id: string) => {
      setBusy(`reopen:${id}`)
      setErr('')
      const { error } = await api.POST('/garden/proposals/reopen', {
        params: { header: workspaceHeader() },
        body: { kind, id },
      })
      setBusy(null)
      setDone((m) => {
        const next = { ...m }
        delete next[id]
        return next
      })
      await reload()
      if (error) setErr(errMessage(error))
    },
    [reload],
  )

  const revertAgent = useCallback(
    async (decisionId: string) => {
      setBusy(`revert:${decisionId}`)
      setErr('')
      const { error } = await api.POST('/garden/delegation/revert', {
        params: { header: workspaceHeader() },
        body: { decision_id: decisionId },
      })
      setBusy(null)
      await reload()
      if (error) setErr(errMessage(error))
    },
    [reload],
  )

  // Suggested work: each action creates a proposal, and the person reads the
  // result where it is decided.
  const synthesise = useCallback(
    async (n: CandidateNode) => {
      setBusy(`work:${n.kind}:${n.note_ids.join(',')}`)
      setErr('')
      const res =
        n.kind === 'distill'
          ? await api.POST('/notes/{note_id}/distill', {
              params: { header: workspaceHeader(), path: { note_id: n.note_ids[0] } },
            })
          : n.kind === 'pattern'
            ? await api.POST('/garden/synthesize/pattern', {
                params: { header: workspaceHeader() },
                body: { source_note_ids: n.note_ids },
              })
            : await api.POST('/garden/synthesize/season', {
                params: { header: workspaceHeader() },
                body: { year: n.year ?? 0, quarter: n.quarter ?? 0 },
              })
      setBusy(null)
      if (res.error || !res.data) {
        setErr(errMessage(res.error))
        return
      }
      const data = res.data as {
        distilled_note_id?: string
        pattern_note_id?: string
        season_note_id?: string
      }
      const id = data.distilled_note_id ?? data.pattern_note_id ?? data.season_note_id
      if (id) navigate(`/garden/review/${id}`)
    },
    [navigate],
  )

  const proposeLink = useCallback(
    async (e: CandidateEdge) => {
      setBusy(`edge:${e.src_note_id}:${e.dst_note_id}`)
      setErr('')
      const { error } = await api.POST('/garden/link-proposals', {
        params: { header: workspaceHeader() },
        body: {
          source_note_id: e.src_note_id,
          target_note_id: e.dst_note_id,
          evidence: { reason: e.reason },
          evidence_ref: e.reason,
        },
      })
      setBusy(null)
      if (error) {
        setErr(errMessage(error))
        return
      }
      await reload()
      setParams({ tab: 'pending' })
    },
    [reload, setParams],
  )

  const when = (iso: string | null | undefined) =>
    iso
      ? new Date(iso).toLocaleString(i18n.language, {
          day: 'numeric',
          month: 'short',
          hour: '2-digit',
          minute: '2-digit',
        })
      : ''

  const shownPending = useMemo(
    () => (filter === 'all' ? pending : pending.filter((p) => p.kind === filter)),
    [pending, filter],
  )
  const countOf = (k: KindFilter) =>
    k === 'all' ? pending.length : pending.filter((p) => p.kind === k).length
  const doable = nodes.filter((n) => n.kind !== 'season' || (n.year && n.quarter))
  const addEdges = edges.filter((e) => e.op === 'add')
  const notHere = edges.length - addEdges.length
  const counts: Record<Tab, string> = {
    pending: `${pending.length}${pendingMore ? '+' : ''}`,
    decided: `${decided.length}${decidedMore ? '+' : ''}`,
    agents: String(agents.filter((a) => !a.reversed_at).length),
    work: String(doable.length + addEdges.length),
  }

  function what(p: Proposal) {
    if (p.kind === 'synthesis') {
      return (
        <>
          <Link to={`/garden/review/${p.id}`} className="gprop__title">
            {p.title || t('gardenProposal.untitled')}
          </Link>
          {p.preview && <span className="gprop__preview">{p.preview}</span>}
        </>
      )
    }
    if (p.kind === 'link') {
      const [a, b] = p.subjects
      return (
        <>
          <span className="gprop__title">
            {t('proposals.linkWhat')}{' '}
            <Link to={`/notes/${a?.note_id}`}>{a?.title || t('gardenProposal.untitled')}</Link>
            {' — '}
            <Link to={`/notes/${b?.note_id}`}>{b?.title || t('gardenProposal.untitled')}</Link>
          </span>
          {p.reason && <span className="gprop__preview">{p.reason}</span>}
        </>
      )
    }
    const src = p.subjects[0]
    return (
      <>
        <span className="gprop__title">
          {p.fact_subject} <span className="chip">{(p.fact_predicate ?? '').replace(/_/g, ' ')}</span>{' '}
          {p.fact_object}
        </span>
        {src && (
          <span className="gprop__preview">
            {t('proposals.factFrom')}{' '}
            <Link to={`/notes/${src.note_id}`}>{src.title || t('gardenProposal.untitled')}</Link>
          </span>
        )}
      </>
    )
  }

  function kindLabel(p: Proposal): string {
    if (p.kind === 'synthesis') return t(`gardenProposal.kind.${p.humus_kind ?? 'distillation'}`)
    return t(`proposals.kind.${p.kind}`)
  }

  function pendingActions(p: Proposal) {
    const b = busy === `decide:${p.id}`
    if (p.kind === 'synthesis') {
      return (
        <>
          <CopyIdButton
            id={p.id}
            label={t('gardenProposal.copyId')}
            copiedLabel={t('gardenProposal.idCopied')}
          />
          <Link to={`/garden/review/${p.id}`} className="btn btn--sm">
            {t('gardenReview.readAndDecide')}
          </Link>
        </>
      )
    }
    if (p.kind === 'link') {
      return (
        <>
          <label className="gprop__kind">
            <span className="sr-only">{t('proposals.linkKind')}</span>
            <select
              value={linkKind[p.id] ?? p.link_kinds?.[0] ?? 'related'}
              onChange={(e) => setLinkKind((m) => ({ ...m, [p.id]: e.target.value }))}
            >
              {(p.link_kinds ?? []).map((k) => (
                <option key={k} value={k}>
                  {t(`garden.mindmap.linkKind.${k}`)}
                </option>
              ))}
            </select>
          </label>
          <button type="button" className="btn--sm" disabled={b} onClick={() => void decide(p, 'accept')}>
            {t('proposals.link.accept')}
          </button>
          <button
            type="button"
            className="btn--ghost btn--sm"
            disabled={b}
            onClick={() => void decide(p, 'decline')}
          >
            {t('proposals.link.decline')}
          </button>
        </>
      )
    }
    return (
      <>
        <button type="button" className="btn--sm" disabled={b} onClick={() => void decide(p, 'accept')}>
          {t('proposals.fact.accept')}
        </button>
        <button
          type="button"
          className="btn--ghost btn--sm"
          disabled={b}
          onClick={() => void decide(p, 'decline')}
        >
          {t('proposals.fact.decline')}
        </button>
      </>
    )
  }

  function row(p: Proposal, listing: 'pending' | 'decided') {
    const justDone = done[p.id]
    return (
      <li key={`${p.kind}:${p.id}`} className="gprop">
        <div className="gprop__head">
          <span className="chip">{kindLabel(p)}</span>
          {p.proposer && <span className="muted">{t('proposals.by', { who: p.proposer })}</span>}
          <span className="muted">
            {when(listing === 'decided' ? (p.decided_at ?? p.created_at) : p.created_at)}
          </span>
          {listing === 'decided' && (
            <span className={`chip gprop__state gprop__state--${p.state}`}>
              {t(`proposals.state.${p.kind}.${p.state}`)}
            </span>
          )}
        </div>
        <div className="gprop__what">{what(p)}</div>
        <div className="gprop__actions" role="group" aria-label={t('gardenProposal.actions')}>
          {justDone ? (
            <>
              <span className="ok" role="status">
                {t(`proposals.done.${justDone.kind}.${justDone.verdict}`)}
              </span>
              <button
                type="button"
                className="btn--ghost btn--sm"
                disabled={busy === `reopen:${p.id}`}
                onClick={() => void reopen(p.kind, p.id)}
              >
                {t('proposals.undo')}
              </button>
            </>
          ) : listing === 'pending' ? (
            pendingActions(p)
          ) : p.reopenable ? (
            <button
              type="button"
              className="btn--ghost btn--sm"
              disabled={busy === `reopen:${p.id}`}
              onClick={() => void reopen(p.kind, p.id)}
            >
              {t('proposals.reopen')}
            </button>
          ) : (
            <span className="hint">{t('proposals.notReopenable')}</span>
          )}
        </div>
      </li>
    )
  }

  return (
    <section className="card card--wide ghreview" aria-labelledby="ghreview-title">
      <header className="ghreview__head">
        <h1 id="ghreview-title">{t('proposals.title')}</h1>
        <Link to="/garden" className="ghreview__back">
          {t('gardenReview.back')}
        </Link>
      </header>
      <p className="ghreview__intro">{t('proposals.intro')}</p>

      <div className="tabs ghreview__tabs" role="tablist" aria-label={t('proposals.title')}>
        {TABS.map((x) => (
          <button
            key={x}
            type="button"
            role="tab"
            aria-selected={tab === x}
            className={`tabs__tab${tab === x ? ' is-active' : ''}`}
            onClick={() => setParams({ tab: x })}
          >
            {t(`proposals.tab.${x}`)} <span className="muted">({counts[x]})</span>
          </button>
        ))}
      </div>

      {removed !== undefined && (
        <p className="hint" role="status">
          {t('gardenReview.removed', { title: removed || t('gardenProposal.untitled') })}
        </p>
      )}
      {err && <p className="error">{err}</p>}

      {loading ? (
        <p className="hint">{t('common.loading')}</p>
      ) : (
        <div role="tabpanel" className="ghreview__panel">
          {tab === 'pending' && (
            <>
              <p className="hint">{t('proposals.hint.pending')}</p>
              <div className="ghreview__filters" role="group" aria-label={t('proposals.filter')}>
                {KIND_FILTERS.map((k) => (
                  <button
                    key={k}
                    type="button"
                    className={`chip${filter === k ? ' is-active' : ''}`}
                    aria-pressed={filter === k}
                    onClick={() => setFilter(k)}
                  >
                    {t(`proposals.filterKind.${k}`)} ({countOf(k)})
                  </button>
                ))}
              </div>
              {shownPending.length === 0 ? (
                <p className="ghreview__empty">{t('proposals.empty.pending')}</p>
              ) : (
                <ul className="gprop__list">{shownPending.map((p) => row(p, 'pending'))}</ul>
              )}
              {pendingMore && (
                <button type="button" className="btn--ghost btn--sm" onClick={() => void more('pending')}>
                  {t('proposals.more')}
                </button>
              )}
            </>
          )}

          {tab === 'decided' && (
            <>
              <p className="hint">{t('proposals.hint.decided')}</p>
              {decided.length === 0 ? (
                <p className="ghreview__empty">{t('proposals.empty.decided')}</p>
              ) : (
                <ul className="gprop__list">{decided.map((p) => row(p, 'decided'))}</ul>
              )}
              {decidedMore && (
                <button type="button" className="btn--ghost btn--sm" onClick={() => void more('decided')}>
                  {t('proposals.more')}
                </button>
              )}
            </>
          )}

          {tab === 'agents' && (
            <>
              <p className="hint">{t('gardenReview.agent.hint')}</p>
              {agents.length === 0 ? (
                <p className="ghreview__empty">{t('proposals.empty.agents')}</p>
              ) : (
                <ul className="gprop__list">
                  {agents.map((d) => (
                    <li key={d.decision_id} className="gprop">
                      <div className="gprop__head">
                        <span className="chip">{t(`gardenDelegation.kind.${d.kind}.name`)}</span>
                        <span className="muted">{when(d.created_at)}</span>
                        {d.reversed_at && (
                          <span className="chip">{t('gardenReview.agent.reversed')}</span>
                        )}
                      </div>
                      <div className="gprop__what">
                        {d.target_kind === 'synthesis' ? (
                          <Link to={`/garden/review/${d.target_id}`} className="gprop__title">
                            {d.target_title || d.target_id.slice(0, 8)}
                          </Link>
                        ) : (
                          <span className="gprop__title">
                            {d.target_title || d.target_id.slice(0, 8)}
                          </span>
                        )}
                        {d.reason && <span className="gprop__preview">{d.reason}</span>}
                      </div>
                      <div className="gprop__actions">
                        {!d.reversed_at && (
                          <button
                            type="button"
                            className="btn--ghost btn--sm"
                            disabled={busy === `revert:${d.decision_id}`}
                            onClick={() => void revertAgent(d.decision_id)}
                          >
                            {t('gardenReview.agent.revert')}
                          </button>
                        )}
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </>
          )}

          {tab === 'work' && (
            <>
              <p className="hint">{t('proposals.hint.work')}</p>
              {doable.length + addEdges.length === 0 ? (
                <p className="ghreview__empty">{t('proposals.empty.work')}</p>
              ) : (
                <ul className="gprop__list">
                  {doable.map((n) => {
                    const key = `work:${n.kind}:${n.note_ids.join(',')}`
                    return (
                      <li key={key} className="gprop">
                        <div className="gprop__head">
                          <span className="chip">{t(`proposals.work.${n.kind}.label`)}</span>
                        </div>
                        <div className="gprop__what">
                          <span className="gprop__title">{n.title}</span>
                          <span className="gprop__preview">{n.reason}</span>
                        </div>
                        <div className="gprop__actions">
                          {n.kind === 'distill' && (
                            <Link to={`/notes/${n.note_ids[0]}`} className="btn--ghost btn--sm">
                              {t('garden.openNote')}
                            </Link>
                          )}
                          <button
                            type="button"
                            className="btn--sm"
                            disabled={busy === key}
                            onClick={() => void synthesise(n)}
                          >
                            {t(`proposals.work.${n.kind}.action`)}
                          </button>
                        </div>
                      </li>
                    )
                  })}
                  {addEdges.map((e) => {
                    const key = `edge:${e.src_note_id}:${e.dst_note_id}`
                    return (
                      <li key={key} className="gprop">
                        <div className="gprop__head">
                          <span className="chip">{t('proposals.work.link.label')}</span>
                        </div>
                        <div className="gprop__what">
                          <span className="gprop__title">
                            <Link to={`/notes/${e.src_note_id}`}>{e.src_title}</Link>
                            {' — '}
                            <Link to={`/notes/${e.dst_note_id}`}>{e.dst_title}</Link>
                          </span>
                          <span className="gprop__preview">{e.reason}</span>
                        </div>
                        <div className="gprop__actions">
                          <button
                            type="button"
                            className="btn--sm"
                            disabled={busy === key}
                            onClick={() => void proposeLink(e)}
                          >
                            {t('proposals.work.link.action')}
                          </button>
                        </div>
                      </li>
                    )
                  })}
                </ul>
              )}
              {notHere > 0 && <p className="hint">{t('proposals.work.notHere', { count: notHere })}</p>}
            </>
          )}
        </div>
      )}
    </section>
  )
}
