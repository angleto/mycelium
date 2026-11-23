import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import type { components, NoteLinkKind } from '../shared'
import { notesByIds } from '../api/notes'

type Proposal = components['schemas']['GardenProposalItem']
type Note = components['schemas']['NoteListOut']

const KINDS: NoteLinkKind[] = ['related', 'hypha_of', 'supersedes', 'contradicts']

// ADR-0073 decision 7: links nobody wrote, proposed by a named proposer and
// decided here by a person. Accepting creates a typed link of the kind the
// person picks and never edits a body; rejecting is remembered, so the pair
// is not proposed again. The proposer's score orders the list and is not
// shown: a confidence beside the proposal would anchor the decision it is
// meant to measure. Each load records what was shown, in order.
export function LinkProposalsPanel({ noteId }: { noteId: string }) {
  const { t } = useTranslation()
  const [rows, setRows] = useState<Proposal[]>([])
  const [notes, setNotes] = useState<Note[]>([])
  const [kind, setKind] = useState<Record<string, NoteLinkKind>>({})
  const [err, setErr] = useState<string | null>(null)
  const recorded = useRef<string>('')

  const load = useCallback(async () => {
    const { data, error } = await api.GET('/garden/link-proposals', {
      params: { header: workspaceHeader(), query: { note_id: noteId, limit: 10 } },
    })
    if (error) return { rows: null, error }
    const list = data?.proposals ?? []
    const others = list.map((p) =>
      p.source_note_id === noteId ? p.target_note_id : p.source_note_id,
    )
    return { rows: list, labels: await notesByIds(others), error: null }
  }, [noteId])

  const apply = useCallback(
    async (res: Awaited<ReturnType<typeof load>>) => {
      if (res.error) {
        setErr(errMessage(res.error))
        return
      }
      setErr(null)
      const list = res.rows ?? []
      setRows(list)
      setNotes(res.labels ?? [])
      // Exposure, once per distinct list: what a person saw, in what order.
      const key = list.map((p) => p.proposal_id).join(',')
      if (list.length > 0 && key !== recorded.current) {
        recorded.current = key
        await api.POST('/garden/link-proposals/shown', {
          params: { header: workspaceHeader() },
          body: { proposal_ids: list.map((p) => p.proposal_id), surface: 'note_panel' },
        })
      }
    },
    [],
  )

  useEffect(() => {
    let active = true
    void (async () => {
      const res = await load()
      if (active) await apply(res)
    })()
    return () => {
      active = false
    }
  }, [load, apply])

  function other(p: Proposal): string {
    return p.source_note_id === noteId ? p.target_note_id : p.source_note_id
  }

  function titleOf(id: string): string {
    const n = notes.find((x) => x.id === id)
    return n?.title?.trim() || (n?.preview ?? '').trim() || t('linkProposals.untitled')
  }

  async function decide(p: Proposal, accept: boolean) {
    setErr(null)
    const { error } = accept
      ? await api.POST('/garden/link-proposals/accept', {
          params: { header: workspaceHeader() },
          body: { proposal_id: p.proposal_id, kind: kind[p.proposal_id] ?? 'related' },
        })
      : await api.POST('/garden/link-proposals/reject', {
          params: { header: workspaceHeader() },
          body: { proposal_id: p.proposal_id },
        })
    if (error) {
      setErr(errMessage(error))
      return
    }
    await apply(await load())
  }

  if (rows.length === 0 && !err) return null
  return (
    <div className="linkedpanel">
      <div className="linkedpanel__head">
        <h3 className="linkedpanel__h">{t('linkProposals.title')}</h3>
        <span className="muted">{t('linkProposals.headHint')}</span>
      </div>
      {err && <p className="error">{err}</p>}
      <ul className="linkedpanel__list">
        {rows.map((p) => {
          const quote = typeof p.evidence?.quote === 'string' ? p.evidence.quote : ''
          return (
            <li key={p.proposal_id} className="linkedpanel__item writtenref">
              <div className="writtenref__body">
                <Link className="linkedpanel__title" to={`/notes/${other(p)}`}>
                  {titleOf(other(p))}
                </Link>
                {quote && <q className="writtenref__quote">{quote}</q>}
                <span className="muted">
                  {t('linkProposals.by', { proposer: p.proposer })}
                </span>
              </div>
              <select
                aria-label={t('linkProposals.kind')}
                value={kind[p.proposal_id] ?? 'related'}
                onChange={(e) =>
                  setKind((k) => ({ ...k, [p.proposal_id]: e.target.value as NoteLinkKind }))
                }
              >
                {KINDS.map((k) => (
                  <option key={k} value={k}>
                    {t(`garden.mindmap.linkKind.${k}`)}
                  </option>
                ))}
              </select>
              <button type="button" className="btn--sm" onClick={() => void decide(p, true)}>
                {t('linkProposals.accept')}
              </button>
              <button
                type="button"
                className="btn--ghost btn--sm"
                title={t('linkProposals.rejectHint')}
                onClick={() => void decide(p, false)}
              >
                {t('linkProposals.reject')}
              </button>
            </li>
          )
        })}
      </ul>
    </div>
  )
}
