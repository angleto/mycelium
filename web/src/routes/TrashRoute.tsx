import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import { useSession } from '../auth/useSession'
import { TagChip } from '../components/TagChip'
import type { components } from '../api/schema'

type Task = components['schemas']['TaskOut']
type NoteT = components['schemas']['NoteOut']
type Mode = 'deleted' | 'archived'

// Recycle bin: soft-deleted tasks (undelete = restore, clears
// deleted_at) and archived tasks (unarchive). Both are reversible
// server-side; this is the UI to reverse them.
export function TrashRoute() {
  const { t } = useTranslation()
  const session = useSession()
  const activeId = session?.workspaceId
  const [tasks, setTasks] = useState<Task[]>([])
  const [notes, setNotes] = useState<NoteT[]>([])
  const [mode, setMode] = useState<Mode>('deleted')
  const [err, setErr] = useState<string | null>(null)

  const load = useCallback(async () => {
    setErr(null)
    const q = { include_deleted: true, include_archived: true }
    const [tk, nt] = await Promise.all([
      api.GET('/tasks', { params: { header: workspaceHeader(), query: q } }),
      api.GET('/notes', { params: { header: workspaceHeader(), query: q } }),
    ])
    if (tk.error || !tk.data) {
      setErr(errMessage(tk.error))
      return
    }
    setTasks(tk.data)
    if (nt.data) setNotes(nt.data)
  }, [])

  useEffect(() => {
    let active = true
    void (async () => {
      const q = { include_deleted: true, include_archived: true }
      const [tk, nt] = await Promise.all([
        api.GET('/tasks', { params: { header: workspaceHeader(), query: q } }),
        api.GET('/notes', { params: { header: workspaceHeader(), query: q } }),
      ])
      if (!active) return
      if (tk.data) setTasks(tk.data)
      if (nt.data) setNotes(nt.data)
    })()
    return () => {
      active = false
    }
  }, [activeId])

  async function restore(tk: Task) {
    setErr(null)
    const { error } = await api.POST('/tasks/{task_id}/restore', {
      params: { header: workspaceHeader(), path: { task_id: tk.id } },
      body: { expected_version: tk.version },
    })
    if (error) setErr(errMessage(error))
    await load()
  }

  async function unarchive(tk: Task) {
    setErr(null)
    const { error } = await api.POST('/tasks/{task_id}/unarchive', {
      params: { header: workspaceHeader(), path: { task_id: tk.id } },
      body: { expected_version: tk.version },
    })
    if (error) setErr(errMessage(error))
    await load()
  }

  async function noteAction(n: NoteT) {
    setErr(null)
    const { error } =
      mode === 'deleted'
        ? await api.POST('/notes/{note_id}/restore', {
            params: { header: workspaceHeader(), path: { note_id: n.id } },
            body: { expected_version: n.version },
          })
        : await api.POST('/notes/{note_id}/unarchive', {
            params: { header: workspaceHeader(), path: { note_id: n.id } },
            body: { expected_version: n.version },
          })
    if (error) setErr(errMessage(error))
    await load()
  }

  const deleted = tasks.filter((t) => t.deleted_at != null)
  const archived = tasks.filter((t) => t.is_archived && t.deleted_at == null)
  const shown = mode === 'deleted' ? deleted : archived
  const nDeleted = notes.filter((n) => n.deleted_at != null)
  const nArchived = notes.filter(
    (n) => n.is_archived && n.deleted_at == null,
  )
  const shownNotes = mode === 'deleted' ? nDeleted : nArchived

  return (
    <section className="card">
      <h1>{t('trash.title')}</h1>
      <div className="row">
        <button
          type="button"
          className={mode === 'deleted' ? 'btn--sm' : 'btn--ghost btn--sm'}
          onClick={() => setMode('deleted')}
        >
          {t('trash.deleted')} ({deleted.length + nDeleted.length})
        </button>
        <button
          type="button"
          className={mode === 'archived' ? 'btn--sm' : 'btn--ghost btn--sm'}
          onClick={() => setMode('archived')}
        >
          {t('trash.archived')} ({archived.length + nArchived.length})
        </button>
      </div>
      {err && <p className="err">{err}</p>}
      {shown.length === 0 && shownNotes.length === 0 ? (
        <p className="hint">{t('trash.none')}</p>
      ) : (
        <ul className="list tasklist">
          {shown.map((tk) => (
            <li key={tk.id} className="taskrow">
              <Link to={`/tasks/${tk.id}`} className="taskrow__title">
                {tk.title}
              </Link>
              <span className="taskrow__tags">
                {(tk.tags ?? []).map((g) => (
                  <TagChip
                    key={g.id}
                    name={g.name}
                    color={g.color}
                    kind={g.kind}
                  />
                ))}
              </span>
              <span className="taskrow__meta">
                <span className="muted">{tk.state}</span>
                <button
                  type="button"
                  className="btn--sm"
                  onClick={() =>
                    void (mode === 'deleted' ? restore(tk) : unarchive(tk))
                  }
                >
                  {mode === 'deleted'
                    ? t('trash.undelete')
                    : t('trash.unarchive')}
                </button>
              </span>
            </li>
          ))}
        </ul>
      )}
      {shownNotes.length > 0 && (
        <>
          <h2>{t('notes.nav')}</h2>
          <ul className="list tasklist">
            {shownNotes.map((n) => (
              <li key={n.id} className="taskrow">
                <span className="taskrow__title">
                  {n.title || n.kind}{' '}
                  <span className="muted">· {n.kind}</span>
                </span>
                <span className="taskrow__meta">
                  <button
                    type="button"
                    className="btn--sm"
                    onClick={() => void noteAction(n)}
                  >
                    {mode === 'deleted'
                      ? t('trash.undelete')
                      : t('trash.unarchive')}
                  </button>
                </span>
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  )
}
