import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api, authFetch, errMessage, workspaceHeader } from '../api/client'
import type { components } from '../api/schema'
import { useWorkflowStates } from '../lib/useWorkflowStates'
import { GardenIcon, type GardenIconName } from './GardenIcon'
import { TaskPickList } from './TaskPickList'

type NoteTaskLink = components['schemas']['NoteTaskLinkOut']
type Task = components['schemas']['TaskOut']
type Tag = components['schemas']['TagOut']

type Kind = 'subject' | 'artifact' | 'derived_from' | 'promoted_from'

// Map each link kind to its forest-metaphor glyph (task 56d80038):
// subject = the task that 'points at' the note (references arc);
// artifact = the leaf-result of the work; derived_from = root-spread
// (the note is the soil the task grew out of); promoted_from = the
// promoted note that branched into a task.
const KIND_ICON: Record<Kind, GardenIconName> = {
  subject: 'references',
  artifact: 'leaf',
  derived_from: 'derives_from',
  promoted_from: 'branch',
}

const KINDS: readonly Kind[] = [
  'subject',
  'artifact',
  'derived_from',
  'promoted_from',
] as const

// Lato nota: pannello "Linked tasks" con quattro sezioni, una per kind
// del modello N:M ``note_task_link``. ``subject`` e ``artifact``
// accettano link da/verso task esistenti (button +); ``derived_from``
// e ``promoted_from`` sono creazione-con-link (si producono solo via
// "Derive task" / "Promote" dal corpo della nota, non da picker).
// ``promoted_from`` è anche non rimovibile via × (annullerebbe la
// promozione lasciando ``note.promoted_at`` orfano: lo impone il
// service layer; il bottone è disabilitato per chiarezza).
export function LinkedTasksPanel({ noteId }: { noteId: string }) {
  const { t } = useTranslation()
  const [links, setLinks] = useState<NoteTaskLink[]>([])
  const [tasks, setTasks] = useState<Task[]>([])
  const [tags, setTags] = useState<Tag[]>([])
  const [adding, setAdding] = useState<Kind | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const wfStates = useWorkflowStates()

  const reload = useCallback(async () => {
    setErr(null)
    const { data, error } = await api.GET('/notes/{note_id}/links', {
      params: { header: workspaceHeader(), path: { note_id: noteId } },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    if (data) setLinks(data.task_links ?? [])
  }, [noteId])

  useEffect(() => {
    let active = true
    void (async () => {
      const [linksRes, tk, tg] = await Promise.all([
        api.GET('/notes/{note_id}/links', {
          params: { header: workspaceHeader(), path: { note_id: noteId } },
        }),
        api.GET('/tasks', { params: { header: workspaceHeader() } }),
        api.GET('/tags', { params: { header: workspaceHeader() } }),
      ])
      if (!active) return
      if (linksRes.data) setLinks(linksRes.data.task_links ?? [])
      if (tk.data) setTasks(tk.data)
      if (tg.data) setTags(tg.data)
    })()
    return () => {
      active = false
    }
  }, [noteId])

  const titleOf = useCallback(
    (taskId: string) =>
      tasks.find((tk) => tk.id === taskId)?.title ??
      t('linkedTasks.unknownTask'),
    [tasks, t],
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

  // ``promoted_from`` and ``derived_from`` are creation-with-link
  // operations: existing tasks can't be wired into them after the
  // fact. Only ``subject`` / ``artifact`` get a + picker.
  const canAdd = (kind: Kind) => kind === 'subject' || kind === 'artifact'
  // The service refuses unlinking a ``promoted_from`` row because the
  // matching ``note.promoted_at`` side-effect has no symmetric unmake.
  const canRemove = (kind: Kind) => kind !== 'promoted_from'

  async function addLink(kind: Kind, taskId: string) {
    setErr(null)
    const res = await authFetch(`/notes/${noteId}/task-links`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ task_id: taskId, kind }),
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

  async function removeLink(kind: Kind, taskId: string) {
    setErr(null)
    const qs = new URLSearchParams({ task_id: taskId, kind })
    const res = await authFetch(`/notes/${noteId}/task-links?${qs.toString()}`, {
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
        <h3 className="linkedpanel__h">{t('linkedTasks.title')}</h3>
        <span className="muted">{t('linkedTasks.headHint')}</span>
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
                  title={t('linkedTasks.add', {
                    kind: t(`taskLinkKind.${kind}`),
                  })}
                >
                  {isAdding ? t('common.cancel') : '+'}
                </button>
              )}
            </header>
            {items.length === 0 ? (
              <p className="hint linkedpanel__empty">
                {t('linkedTasks.empty')}
              </p>
            ) : (
              <ul className="linkedpanel__list">
                {items.map((link) => (
                  <li key={link.id} className="linkedpanel__item">
                    <Link
                      className="linkedpanel__title"
                      to={`/tasks/${link.task_id}`}
                    >
                      {titleOf(link.task_id)}
                    </Link>
                    <button
                      type="button"
                      className="btn--ghost btn--sm"
                      disabled={!canRemove(kind)}
                      title={
                        canRemove(kind)
                          ? t('linkedTasks.remove')
                          : t('linkedTasks.promotedReadonly')
                      }
                      onClick={() =>
                        canRemove(kind) && void removeLink(kind, link.task_id)
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
                <TaskPickList
                  tasks={tasks.filter(
                    (tk) => !items.some((li) => li.task_id === tk.id),
                  )}
                  tags={tags}
                  states={wfStates}
                  value={null}
                  onPick={(id) => void addLink(kind, id)}
                  placeholder={t('linkedTasks.pickerPh')}
                />
              </div>
            )}
          </section>
        )
      })}
    </div>
  )
}
