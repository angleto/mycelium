import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import { useSession } from '../auth/useSession'
import type { components } from '../api/schema'

type Task = components['schemas']['TaskOut']
type Tag = components['schemas']['TagOut']

// Same task created from GUI/REST/MCP -> identical state (roadmap W1).
// Tags carry a kind (generic|client|project); a project can link a
// client. The list filters by tag_id server-side (consistent filtering).
export function TasksRoute() {
  const { t } = useTranslation()
  const session = useSession()
  const activeId = session?.workspaceId
  const [tasks, setTasks] = useState<Task[]>([])
  const [tags, setTags] = useState<Tag[]>([])
  const [filter, setFilter] = useState('')
  const [title, setTitle] = useState('')
  const [priority, setPriority] = useState(3)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const loadTags = useCallback(async () => {
    const { data } = await api.GET('/tags', { params: { header: workspaceHeader() } })
    if (data) setTags(data)
  }, [])

  const loadTasks = useCallback(async () => {
    setErr(null)
    const { data, error } = await api.GET('/tasks', {
      params: {
        header: workspaceHeader(),
        query: filter ? { tag_id: filter } : {},
      },
    })
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    setTasks(data)
  }, [filter])

  useEffect(() => {
    let active = true
    void (async () => {
      const h = workspaceHeader()
      const [tg, tk] = await Promise.all([
        api.GET('/tags', { params: { header: h } }),
        api.GET('/tasks', {
          params: { header: h, query: filter ? { tag_id: filter } : {} },
        }),
      ])
      if (!active) return
      if (tg.data) setTags(tg.data)
      if (tk.data) setTasks(tk.data)
      else setErr(errMessage(tk.error))
    })()
    return () => {
      active = false
    }
  }, [activeId, filter])

  async function onCreate(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setErr(null)
    const { error } = await api.POST('/tasks', {
      params: { header: workspaceHeader() },
      body: { title, priority, executor_kind: 'human', necessity: 'should' },
    })
    setBusy(false)
    if (error) {
      setErr(errMessage(error))
      return
    }
    setTitle('')
    await loadTasks()
  }

  return (
    <section className="card">
      <h1>{t('tasks.title')}</h1>

      <form onSubmit={(e) => void onCreate(e)} className="row">
        <input
          required
          placeholder={t('tasks.newTitle')}
          value={title}
          onChange={(e) => setTitle(e.target.value)}
        />
        <label>
          {t('tasks.priority')}
          <input
            type="number"
            min={1}
            max={5}
            value={priority}
            onChange={(e) => setPriority(Number(e.target.value))}
          />
        </label>
        <button type="submit" disabled={busy}>
          {busy ? t('tasks.saving') : t('tasks.create')}
        </button>
      </form>

      <label>
        {t('tasks.filterTag')}{' '}
        <select value={filter} onChange={(e) => setFilter(e.target.value)}>
          <option value="">{t('tasks.all')}</option>
          {tags.map((tg) => (
            <option key={tg.id} value={tg.id}>
              {tg.kind}: {tg.name}
            </option>
          ))}
        </select>
      </label>

      {err && <p className="err">{err}</p>}
      {tasks.length === 0 ? (
        <p className="hint">{t('tasks.none')}</p>
      ) : (
        <ul className="list">
          {tasks.map((tk) => (
            <li key={tk.id}>
              <Link to={`/tasks/${tk.id}`}>{tk.title}</Link>
              <span className="muted">
                {' '}
                · {tk.state} · P{tk.priority}
              </span>
            </li>
          ))}
        </ul>
      )}

      <TaxonomyPanel tags={tags} onChanged={() => void loadTags()} />
    </section>
  )
}

function TaxonomyPanel({
  tags,
  onChanged,
}: {
  tags: Tag[]
  onChanged: () => void
}) {
  const { t } = useTranslation()
  const [tagName, setTagName] = useState('')
  const [clientName, setClientName] = useState('')
  const [ragione, setRagione] = useState('')
  const [projName, setProjName] = useState('')
  const [clientTag, setClientTag] = useState('')
  const [err, setErr] = useState<string | null>(null)

  const clients = tags.filter((x) => x.kind === 'client')

  async function add<T>(p: Promise<{ error?: T }>, reset: () => void) {
    const { error } = await p
    if (error) {
      setErr(errMessage(error))
      return
    }
    setErr(null)
    reset()
    onChanged()
  }

  return (
    <div className="taxonomy">
      <h2>{t('tasks.taxonomy')}</h2>
      {err && <p className="err">{err}</p>}
      <form
        onSubmit={(e) => {
          e.preventDefault()
          void add(
            api.POST('/tags', {
              params: { header: workspaceHeader() },
              body: { kind: 'generic', name: tagName },
            }),
            () => setTagName(''),
          )
        }}
      >
        <input
          required
          placeholder={t('tasks.tagName')}
          value={tagName}
          onChange={(e) => setTagName(e.target.value)}
        />
        <button type="submit">{t('tasks.addGeneric')}</button>
      </form>
      <form
        onSubmit={(e) => {
          e.preventDefault()
          void add(
            api.POST('/clients', {
              params: { header: workspaceHeader() },
              body: { name: clientName, ragione_sociale: ragione },
            }),
            () => {
              setClientName('')
              setRagione('')
            },
          )
        }}
      >
        <input
          required
          placeholder={t('tasks.clientName')}
          value={clientName}
          onChange={(e) => setClientName(e.target.value)}
        />
        <input
          required
          placeholder={t('tasks.ragioneSociale')}
          value={ragione}
          onChange={(e) => setRagione(e.target.value)}
        />
        <button type="submit">{t('tasks.addClient')}</button>
      </form>
      <form
        onSubmit={(e) => {
          e.preventDefault()
          void add(
            api.POST('/projects', {
              params: { header: workspaceHeader() },
              body: {
                name: projName,
                client_tag_id: clientTag || null,
                valuta: 'EUR',
              },
            }),
            () => setProjName(''),
          )
        }}
      >
        <input
          required
          placeholder={t('tasks.projectName')}
          value={projName}
          onChange={(e) => setProjName(e.target.value)}
        />
        <select value={clientTag} onChange={(e) => setClientTag(e.target.value)}>
          <option value="">{t('tasks.all')}</option>
          {clients.map((c) => (
            <option key={c.id} value={c.id}>
              {c.name}
            </option>
          ))}
        </select>
        <button type="submit">{t('tasks.addProject')}</button>
      </form>
    </div>
  )
}
