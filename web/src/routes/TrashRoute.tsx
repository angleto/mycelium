import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import { notesPage } from '../api/notes'
import { useSession } from '../auth/useSession'
import { useMyWorkspace } from '../auth/useMyWorkspace'
import { TagChip } from '../components/TagChip'
import type { components } from '../shared'

type Task = components['schemas']['TaskOut']
type NoteT = components['schemas']['NoteListOut']
type Mode = 'deleted' | 'archived'

// Recycle bin: soft-deleted tasks (undelete = restore, clears
// deleted_at) and archived tasks (unarchive). Both are reversible
// server-side; this is the UI to reverse them.
/** Notes per page in each tab of the bin. */
const PAGE_SIZE = 50

export function TrashRoute() {
  const { t } = useTranslation()
  const session = useSession()
  const activeId = session?.workspaceId
  const { ws } = useMyWorkspace()
  const canEmpty = ws?.my_role === 'owner' || ws?.my_role === 'admin'
  const [tasks, setTasks] = useState<Task[]>([])
  // The notes in the bin and in the archive, each read a page at a time
  // and asked of the server by state. The bin used to take the list's 200
  // newest notes of every state and keep the deleted ones, so a note
  // deleted before those 200 never showed up here.
  const [nDeleted, setNDeleted] = useState<NoteT[]>([])
  const [nArchived, setNArchived] = useState<NoteT[]>([])
  const [more, setMore] = useState<Record<Mode, string | null>>({
    deleted: null,
    archived: null,
  })
  const [mode, setMode] = useState<Mode>('deleted')
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    setErr(null)
    const q = { include_deleted: true, include_archived: true }
    const [tk, del, arc] = await Promise.all([
      api.GET('/tasks', { params: { header: workspaceHeader(), query: q } }),
      notesPage({ only: 'deleted', limit: PAGE_SIZE }),
      notesPage({ only: 'archived', limit: PAGE_SIZE }),
    ])
    if (tk.error || !tk.data) {
      setErr(errMessage(tk.error))
      return
    }
    setTasks(tk.data)
    setNDeleted(del.items)
    setNArchived(arc.items)
    setMore({ deleted: del.next_cursor ?? null, archived: arc.next_cursor ?? null })
  }, [])

  useEffect(() => {
    let active = true
    void (async () => {
      const q = { include_deleted: true, include_archived: true }
      const [tk, del, arc] = await Promise.all([
        api.GET('/tasks', { params: { header: workspaceHeader(), query: q } }),
        notesPage({ only: 'deleted', limit: PAGE_SIZE }),
        notesPage({ only: 'archived', limit: PAGE_SIZE }),
      ])
      if (!active) return
      if (tk.data) setTasks(tk.data)
      setNDeleted(del.items)
      setNArchived(arc.items)
      setMore({ deleted: del.next_cursor ?? null, archived: arc.next_cursor ?? null })
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

  async function emptyTrash() {
    if (!canEmpty) return
    if (!window.confirm(t('trash.emptyConfirm'))) return
    setErr(null)
    setBusy(true)
    try {
      const { data, error } = await api.POST('/workspaces/me/trash/empty', {
        params: { header: workspaceHeader() },
      })
      if (error) {
        setErr(errMessage(error))
      } else if (data) {
        window.alert(
          t('trash.emptyResult', { tasks: data.tasks, notes: data.notes }),
        )
      }
      await load()
    } finally {
      setBusy(false)
    }
  }

  const deleted = tasks.filter((t) => t.deleted_at != null)
  const archived = tasks.filter((t) => t.is_archived && t.deleted_at == null)
  const shown = mode === 'deleted' ? deleted : archived
  const shownNotes = mode === 'deleted' ? nDeleted : nArchived

  // The next page of the notes in the current tab.
  async function loadMoreNotes() {
    const cursor = more[mode]
    if (!cursor) return
    const page = await notesPage({ only: mode, limit: PAGE_SIZE, cursor })
    if (mode === 'deleted') setNDeleted((prev) => [...prev, ...page.items])
    else setNArchived((prev) => [...prev, ...page.items])
    setMore((prev) => ({ ...prev, [mode]: page.next_cursor ?? null }))
  }

  return (
    <section className="card">
      <h1>{t('trash.title')}</h1>
      <div className="row">
        <button
          type="button"
          className={mode === 'deleted' ? 'btn--sm' : 'btn--ghost btn--sm'}
          onClick={() => setMode('deleted')}
        >
          {t('trash.deleted')} ({deleted.length + nDeleted.length}
          {more.deleted ? '+' : ''})
        </button>
        <button
          type="button"
          className={mode === 'archived' ? 'btn--sm' : 'btn--ghost btn--sm'}
          onClick={() => setMode('archived')}
        >
          {t('trash.archived')} ({archived.length + nArchived.length}
          {more.archived ? '+' : ''})
        </button>
        {canEmpty &&
          mode === 'deleted' &&
          deleted.length + nDeleted.length > 0 && (
            <button
              type="button"
              className="btn--sm btn--danger"
              onClick={() => void emptyTrash()}
              disabled={busy}
              style={{ marginLeft: 'auto' }}
            >
              {t('trash.empty')}
            </button>
          )}
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
          {more[mode] && (
            <p className="row">
              <button
                type="button"
                className="btn--ghost"
                onClick={() => void loadMoreNotes()}
              >
                {t('notes.loadMore')}
              </button>
            </p>
          )}
        </>
      )}
    </section>
  )
}
