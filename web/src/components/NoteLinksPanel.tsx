import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api, authFetch, errMessage, workspaceHeader } from '../api/client'
import type { components } from '../api/schema'
import { NotePickList } from './NotePickList'

type NoteLinkOut = components['schemas']['NoteLinkOut']
type Note = components['schemas']['NoteOut']

// Mycelial 4-verb link model (ADR-0040). ``related`` is undirected;
// the other three are directional (parent=origin/superseder/refuter,
// child=derived/superseded/refuted).
type Kind = 'hypha_of' | 'related' | 'supersedes' | 'contradicts'

const KINDS: readonly Kind[] = [
  'hypha_of',
  'related',
  'supersedes',
  'contradicts',
] as const

const DIRECTIONAL: Record<Kind, boolean> = {
  hypha_of: true,
  related: false,
  supersedes: true,
  contradicts: true,
}

// Lato nota: pannello "Linked ideas" che pilota i quattro verbi
// note-to-note di ADR-0040. Mirror strutturale di LinkedTasksPanel
// (sezioni per-kind, toggle ``adding``, NotePickList in
// ``linkedpanel__picker``, authFetch POST/DELETE, raggruppamento
// byKind, canAdd/canRemove). Differenza chiave: la direzionalità.
// Per i kind direzionali distinguiamo outgoing (questa nota = parent,
// ``asParent``) e incoming (questa nota = child, ``asChild``); in
// aggiunta si parte da questa-nota-come-parent con uno swap. Per
// ``related`` (non orientato) si lista e basta: il picker crea il link
// con questa nota come parent_note_id e il server canonicalizza gli
// estremi.
export function NoteLinksPanel({ noteId }: { noteId: string }) {
  const { t } = useTranslation()
  const [outgoing, setOutgoing] = useState<NoteLinkOut[]>([])
  const [incoming, setIncoming] = useState<NoteLinkOut[]>([])
  const [notes, setNotes] = useState<Note[]>([])
  const [adding, setAdding] = useState<Kind | null>(null)
  // When adding a directional link, false = this-note-as-parent
  // (default), true = this-note-as-child (swapped).
  const [addAsChild, setAddAsChild] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const reload = useCallback(async () => {
    setErr(null)
    const { data, error } = await api.GET('/notes/{note_id}/links', {
      params: { header: workspaceHeader(), path: { note_id: noteId } },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    if (data) {
      setOutgoing(data.outgoing ?? [])
      setIncoming(data.incoming ?? [])
    }
  }, [noteId])

  useEffect(() => {
    let active = true
    void (async () => {
      const [linksRes, nt] = await Promise.all([
        api.GET('/notes/{note_id}/links', {
          params: { header: workspaceHeader(), path: { note_id: noteId } },
        }),
        api.GET('/notes', { params: { header: workspaceHeader() } }),
      ])
      if (!active) return
      if (linksRes.data) {
        setOutgoing(linksRes.data.outgoing ?? [])
        setIncoming(linksRes.data.incoming ?? [])
      }
      if (nt.data) setNotes(nt.data)
    })()
    return () => {
      active = false
    }
  }, [noteId])

  const titleOf = useCallback(
    (id: string) => {
      const n = notes.find((x) => x.id === id)
      if (!n) return t('noteLinks.unknownNote')
      return (
        n.title?.trim() ||
        (n.transcript ?? '').split('\n').find((s) => s.trim()) ||
        t('noteLinks.unknownNote')
      )
    },
    [notes, t],
  )

  // Outgoing links group by kind on parent==this side; incoming on the
  // child==this side. ``related`` is merged below.
  const outByKind = useMemo(() => {
    const m: Record<Kind, NoteLinkOut[]> = {
      hypha_of: [],
      related: [],
      supersedes: [],
      contradicts: [],
    }
    for (const link of outgoing) {
      const k = link.kind as Kind
      if (m[k]) m[k].push(link)
    }
    return m
  }, [outgoing])

  const inByKind = useMemo(() => {
    const m: Record<Kind, NoteLinkOut[]> = {
      hypha_of: [],
      related: [],
      supersedes: [],
      contradicts: [],
    }
    for (const link of incoming) {
      const k = link.kind as Kind
      if (m[k]) m[k].push(link)
    }
    return m
  }, [incoming])

  // The other endpoint of a link, relative to this note.
  const otherId = useCallback(
    (link: NoteLinkOut) =>
      link.parent_note_id === noteId
        ? link.child_note_id
        : link.parent_note_id,
    [noteId],
  )

  // System-generated links (e.g. decomposition) carry no created_by;
  // mirror the LinkedTasksPanel ``canRemove`` discipline by marking
  // them read-only rather than offering a delete that the service
  // refuses.
  const isSystem = (link: NoteLinkOut) => !link.created_by

  // Ids already linked to this note for a given kind (either
  // direction), so the picker can exclude them.
  const linkedIdsFor = useCallback(
    (kind: Kind) => {
      const set = new Set<string>()
      for (const l of outByKind[kind]) set.add(otherId(l))
      for (const l of inByKind[kind]) set.add(otherId(l))
      return set
    },
    [outByKind, inByKind, otherId],
  )

  async function addLink(kind: Kind, targetId: string) {
    setErr(null)
    // related is undirected: this note is always parent_note_id, the
    // server canonicalises parent<child. Directional kinds honour the
    // swap toggle.
    const swap = DIRECTIONAL[kind] && addAsChild
    const body = {
      parent_note_id: swap ? targetId : noteId,
      child_note_id: swap ? noteId : targetId,
      kind,
    }
    const res = await authFetch(`/notes/${noteId}/links`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    })
    if (!res.ok) {
      try {
        setErr(errMessage((await res.json()) as unknown))
      } catch {
        setErr(t('error.generic'))
      }
      return
    }
    setAdding(null)
    setAddAsChild(false)
    await reload()
  }

  async function removeLink(kind: Kind, link: NoteLinkOut) {
    setErr(null)
    // DELETE matches by (note_id in path, child_note_id, kind). The
    // child is the non-this endpoint for outgoing links and this note
    // for incoming ones; canonicalised ``related`` rows are matched by
    // whichever endpoint the server stored as child.
    const qs = new URLSearchParams({
      child_note_id: link.child_note_id,
      kind,
    })
    const res = await authFetch(`/notes/${noteId}/links?${qs.toString()}`, {
      method: 'DELETE',
    })
    if (!res.ok && res.status !== 404) {
      try {
        setErr(errMessage((await res.json()) as unknown))
      } catch {
        setErr(t('error.generic'))
      }
      return
    }
    await reload()
  }

  function renderItems(kind: Kind, links: NoteLinkOut[]) {
    if (links.length === 0) {
      return <p className="hint linkedpanel__empty">{t('noteLinks.empty')}</p>
    }
    return (
      <ul className="linkedpanel__list">
        {links.map((link) => {
          const system = isSystem(link)
          return (
            <li key={link.id} className="linkedpanel__item">
              <Link
                className="linkedpanel__title"
                to={`/notes/${otherId(link)}`}
              >
                {titleOf(otherId(link))}
              </Link>
              <button
                type="button"
                className="btn--ghost btn--sm"
                disabled={system}
                title={
                  system
                    ? t('noteLinks.systemReadonly')
                    : t('noteLinks.remove')
                }
                onClick={() => !system && void removeLink(kind, link)}
              >
                ×
              </button>
            </li>
          )
        })}
      </ul>
    )
  }

  return (
    <div className="linkedpanel">
      <div className="linkedpanel__head">
        <h3 className="linkedpanel__h">{t('noteLinks.title')}</h3>
        <span className="muted">{t('noteLinks.headHint')}</span>
      </div>
      {err && <p className="error">{err}</p>}
      {KINDS.map((kind) => {
        const isAdding = adding === kind
        const directional = DIRECTIONAL[kind]
        const out = outByKind[kind]
        const inc = inByKind[kind]
        const total = directional ? out.length + inc.length : out.length
        const excluded = linkedIdsFor(kind)
        return (
          <section key={kind} className="linkedpanel__section">
            <header className="linkedpanel__sectionhead">
              <span
                className={`chip chip--kind chip--kind-${kind}`}
                title={t(`garden.mindmap.linkKindHint.${kind}`)}
              >
                {t(`garden.mindmap.linkKind.${kind}`)}
              </span>
              <span
                className="muted"
                title={t(`garden.mindmap.linkKindHint.${kind}`)}
              >
                (i)
              </span>
              <span className="muted">({total})</span>
              <button
                type="button"
                className="btn--ghost btn--sm"
                onClick={() => {
                  setAddAsChild(false)
                  setAdding(isAdding ? null : kind)
                }}
                title={t('noteLinks.add')}
              >
                {isAdding ? t('common.cancel') : '+'}
              </button>
            </header>
            {directional ? (
              <>
                <div className="linkedpanel__direction">
                  <span className="muted">{t('noteLinks.asParent')}</span>
                  {renderItems(kind, out)}
                </div>
                <div className="linkedpanel__direction">
                  <span className="muted">{t('noteLinks.asChild')}</span>
                  {renderItems(kind, inc)}
                </div>
              </>
            ) : (
              renderItems(kind, out)
            )}
            {isAdding && (
              <div className="linkedpanel__picker">
                {directional && (
                  <div className="row linkedpanel__directionpick">
                    <span className="muted">
                      {addAsChild
                        ? t('noteLinks.asChild')
                        : t('noteLinks.asParent')}
                    </span>
                    <button
                      type="button"
                      className="btn--ghost btn--sm"
                      onClick={() => setAddAsChild((v) => !v)}
                      title={t('noteLinks.swap')}
                    >
                      {t('noteLinks.swap')}
                    </button>
                  </div>
                )}
                <NotePickList
                  notes={notes.filter(
                    (n) => n.id !== noteId && !excluded.has(n.id),
                  )}
                  value={null}
                  onPick={(id) => void addLink(kind, id)}
                  placeholder={t('noteLinks.pickerPh')}
                />
              </div>
            )}
          </section>
        )
      })}
    </div>
  )
}
