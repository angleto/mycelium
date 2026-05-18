import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import { useSession } from '../auth/useSession'
import { TagChip } from '../components/TagChip'
import type { components } from '../api/schema'

type Task = components['schemas']['TaskOut']
type Mode = 'deleted' | 'archived'

// Recycle bin: soft-deleted tasks (undelete = restore, clears
// deleted_at) and archived tasks (unarchive). Both are reversible
// server-side; this is the UI to reverse them.
export function TrashRoute() {
  const { t } = useTranslation()
  const session = useSession()
  const activeId = session?.workspaceId
  const [tasks, setTasks] = useState<Task[]>([])
  const [mode, setMode] = useState<Mode>('deleted')
  const [err, setErr] = useState<string | null>(null)

  const load = useCallback(async () => {
    setErr(null)
    const { data, error } = await api.GET('/tasks', {
      params: {
        header: workspaceHeader(),
        query: { include_deleted: true, include_archived: true },
      },
    })
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    setTasks(data)
  }, [])

  useEffect(() => {
    let active = true
    void (async () => {
      const { data } = await api.GET('/tasks', {
        params: {
          header: workspaceHeader(),
          query: { include_deleted: true, include_archived: true },
        },
      })
      if (active && data) setTasks(data)
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

  const deleted = tasks.filter((t) => t.deleted_at != null)
  const archived = tasks.filter((t) => t.is_archived && t.deleted_at == null)
  const shown = mode === 'deleted' ? deleted : archived

  return (
    <section className="card">
      <h1>{t('trash.title')}</h1>
      <div className="row">
        <button
          type="button"
          className={mode === 'deleted' ? 'btn--sm' : 'btn--ghost btn--sm'}
          onClick={() => setMode('deleted')}
        >
          {t('trash.deleted')} ({deleted.length})
        </button>
        <button
          type="button"
          className={mode === 'archived' ? 'btn--sm' : 'btn--ghost btn--sm'}
          onClick={() => setMode('archived')}
        >
          {t('trash.archived')} ({archived.length})
        </button>
      </div>
      {err && <p className="err">{err}</p>}
      {shown.length === 0 ? (
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
    </section>
  )
}
