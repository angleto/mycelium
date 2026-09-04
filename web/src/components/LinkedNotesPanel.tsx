import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api, authFetch, errMessage, workspaceHeader } from '../api/client'
import type { components } from '../shared'
import { GardenIcon, type GardenIconName } from './GardenIcon'
import { NotePickList } from './NotePickList'

type NoteTaskLink = components['schemas']['NoteTaskLinkOut']
type Note = components['schemas']['NoteListOut']

type Kind = 'subject' | 'artifact' | 'derived_from' | 'promoted_from'

const KINDS: readonly Kind[] = [
  'subject',
  'artifact',
  'derived_from',
  'promoted_from',
] as const

// Mirror of LinkedTasksPanel: same kind -> forest-metaphor glyph
// mapping so the two drawers read identically (task 56d80038).
const KIND_ICON: Record<Kind, GardenIconName> = {
  subject: 'references',
  artifact: 'leaf',
  derived_from: 'derives_from',
  promoted_from: 'branch',
}

// Lato task: pannello "Linked notes" speculare a LinkedTasksPanel.
// Stesse regole per kind: subject/artifact accettano picker;
// derived_from/promoted_from sono creation-with-link (qui mostrate
// in sola lettura, perché il task è il "ricevente" del link e non c'è
// un endpoint task→nota per crearle a posteriori). ``promoted_from``
// resta non rimovibile, ``derived_from`` può essere staccato per
// chiarezza editoriale (il task rimane).
export function LinkedNotesPanel({ taskId }: { taskId: string }) {
  const { t } = useTranslation()
  const [links, setLinks] = useState<NoteTaskLink[]>([])
  const [notes, setNotes] = useState<Note[]>([])
  const [adding, setAdding] = useState<Kind | null>(null)
  const [err, setErr] = useState<string | null>(null)

  const reload = useCallback(async () => {
    setErr(null)
    const res = await authFetch(`/tasks/${taskId}/note-links`)
    if (!res.ok) {
      try {
        setErr(errMessage((await res.json()) as unknown))
      } catch {
        setErr(t('error.generic'))
      }
      return
    }
    const data = (await res.json()) as {
      task_id: string
      note_links: NoteTaskLink[]
    }
    setLinks(data.note_links ?? [])
  }, [taskId, t])

  useEffect(() => {
    let active = true
    void (async () => {
      const [linksRes, ns] = await Promise.all([
        authFetch(`/tasks/${taskId}/note-links`),
        api.GET('/notes', { params: { header: workspaceHeader() } }),
      ])
      if (!active) return
      if (linksRes.ok) {
        const data = (await linksRes.json()) as {
          task_id: string
          note_links: NoteTaskLink[]
        }
        setLinks(data.note_links ?? [])
      }
      if (ns.data) setNotes(ns.data)
    })()
    return () => {
      active = false
    }
  }, [taskId])

  const titleOf = useCallback(
    (noteId: string) => {
      const n = notes.find((x) => x.id === noteId)
      if (!n) return t('linkedNotes.unknownNote')
      return (
        n.title?.trim() ||
        (n.preview ?? '').trim() ||
        t('notes.untitled')
      )
    },
    [notes, t],
  )

  const byKind = useMemo(() => {
    const m: Record<Kind, NoteTaskLink[]> = {
      subject: [],
      artifact: [],
      derived_from: [],
      promoted_from: [],
    }
    for (const link of links) {
      const k = link.kind as Kind
      if (m[k]) m[k].push(link)
    }
    return m
  }, [links])

  const canAdd = (kind: Kind) => kind === 'subject' || kind === 'artifact'
  const canRemove = (kind: Kind) => kind !== 'promoted_from'

  async function addLink(kind: Kind, noteId: string) {
    setErr(null)
    const res = await authFetch(`/tasks/${taskId}/note-links`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ note_id: noteId, kind }),
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
    await reload()
  }

  async function removeLink(kind: Kind, noteId: string) {
    setErr(null)
    const qs = new URLSearchParams({ note_id: noteId, kind })
    const res = await authFetch(`/tasks/${taskId}/note-links?${qs.toString()}`, {
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

  return (
    <div className="linkedpanel">
      <div className="linkedpanel__head">
        <strong>{t('linkedNotes.title')}</strong>
        <span className="muted">{t('linkedNotes.headHint')}</span>
      </div>
      {err && <p className="error">{err}</p>}
      {KINDS.map((kind) => {
        const items = byKind[kind]
        const isAdding = adding === kind
        return (
          <section key={kind} className="linkedpanel__section">
            <header className="linkedpanel__sectionhead">
              <span className={`chip chip--kind chip--kind-${kind}`}>
                <GardenIcon name={KIND_ICON[kind]} size={14} />
                {t(`taskLinkKind.${kind}`)}
              </span>
              <span className="muted">({items.length})</span>
              {canAdd(kind) && (
                <button
                  type="button"
                  className="btn--ghost btn--sm"
                  onClick={() => setAdding(isAdding ? null : kind)}
                  title={t('linkedNotes.add', {
                    kind: t(`taskLinkKind.${kind}`),
                  })}
                >
                  {isAdding ? t('common.cancel') : '+'}
                </button>
              )}
            </header>
            {items.length === 0 ? (
              <p className="hint linkedpanel__empty">
                {t('linkedNotes.empty')}
              </p>
            ) : (
              <ul className="linkedpanel__list">
                {items.map((link) => (
                  <li key={link.id} className="linkedpanel__item">
                    <Link
                      className="linkedpanel__title"
                      to={`/notes?open=${link.note_id}`}
                    >
                      {titleOf(link.note_id)}
                    </Link>
                    <button
                      type="button"
                      className="btn--ghost btn--sm"
                      disabled={!canRemove(kind)}
                      title={
                        canRemove(kind)
                          ? t('linkedNotes.remove')
                          : t('linkedTasks.promotedReadonly')
                      }
                      onClick={() =>
                        canRemove(kind) && void removeLink(kind, link.note_id)
                      }
                    >
                      ×
                    </button>
                  </li>
                ))}
              </ul>
            )}
            {isAdding && (
              <div className="linkedpanel__picker">
                <NotePickList
                  notes={notes.filter(
                    (n) => !items.some((li) => li.note_id === n.id),
                  )}
                  value={null}
                  onPick={(id) => void addLink(kind, id)}
                  placeholder={t('linkedNotes.pickerPh')}
                />
              </div>
            )}
          </section>
        )
      })}
    </div>
  )
}
