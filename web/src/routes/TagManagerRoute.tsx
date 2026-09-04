import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type FormEvent,
} from 'react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import { useSession } from '../auth/useSession'
import { useFocus } from '../lib/focus'
import { TagChip } from '../components/TagChip'
import type { components } from '../shared'

type Tag = components['schemas']['TagOut']

function TagRow({
  tag,
  targets,
  onChanged,
}: {
  tag: Tag
  targets: Tag[]
  onChanged: () => void
}) {
  const { t } = useTranslation()
  const [name, setName] = useState(tag.name)
  const [color, setColor] = useState(tag.color || '#4a6b3e')
  const [scope, setScope] = useState<string[]>(tag.scope_target_ids ?? [])
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const archived = tag.status === 'archived'
  const targetName = (id: string) =>
    targets.find((x) => x.id === id)?.name ?? id.slice(0, 8)

  async function saveScope(next: string[]) {
    setErr(null)
    setMsg(null)
    const { error } = await api.PUT('/tags/{tag_id}/scope', {
      params: { header: workspaceHeader(), path: { tag_id: tag.id } },
      body: { target_ids: next },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    setScope(next)
    setMsg(t('tagmgr.saved'))
    onChanged()
  }

  async function patch(body: Record<string, unknown>) {
    setErr(null)
    setMsg(null)
    const { error, response } = await api.PATCH('/tags/{tag_id}', {
      params: { header: workspaceHeader(), path: { tag_id: tag.id } },
      body: { expected_version: tag.version, ...body },
    })
    if (response.status === 409) {
      setErr(t('tagmgr.conflict'))
      onChanged()
      return
    }
    if (error) {
      setErr(errMessage(error))
      return
    }
    setMsg(t('tagmgr.saved'))
    onChanged()
  }

  return (
    <li>
      <TagChip name={name} color={color} kind={tag.kind} />
      <input value={name} onChange={(e) => setName(e.target.value)} />
      <input
        type="color"
        value={/^#[0-9a-fA-F]{6}$/.test(color) ? color : '#4a6b3e'}
        onChange={(e) => setColor(e.target.value)}
        aria-label={t('tagmgr.color')}
      />
      <button
        type="button"
        className="btn--sm"
        onClick={() => void patch({ name, color })}
      >
        {t('tagmgr.save')}
      </button>
      {archived ? (
        <button
          type="button"
          className="btn--sm"
          onClick={() => void patch({ status: 'active' })}
        >
          {t('tagmgr.unarchive')}
        </button>
      ) : (
        <button
          type="button"
          className="btn--ghost btn--sm"
          onClick={() => void patch({ status: 'archived' })}
        >
          {t('tagmgr.archive')}
        </button>
      )}
      <span className="muted">
        {tag.kind}
        {archived ? ` · ${t('tagmgr.archived')}` : ''}
      </span>
      {msg && <span className="ok">{msg}</span>}
      {err && <span className="err">{err}</span>}
      <div className="chips" style={{ marginTop: '0.35rem' }}>
        <span className="muted">{t('tagmgr.scope')}:</span>
        {scope.length === 0 && (
          <span className="muted">{t('tagmgr.global')}</span>
        )}
        {scope.map((id) => (
          <button
            key={id}
            type="button"
            className="chip chip--rm"
            title={t('tagmgr.scopeRemove')}
            onClick={() => void saveScope(scope.filter((x) => x !== id))}
          >
            {targetName(id)} ✕
          </button>
        ))}
        <select
          value=""
          onChange={(e) =>
            e.target.value && void saveScope([...scope, e.target.value])
          }
        >
          <option value="">{t('tagmgr.scopeAdd')}</option>
          {targets
            .filter((x) => !scope.includes(x.id))
            .map((x) => (
              <option key={x.id} value={x.id}>
                {x.kind}: {x.name}
              </option>
            ))}
        </select>
      </div>
    </li>
  )
}

export function TagManagerRoute() {
  const { t } = useTranslation()
  const session = useSession()
  const activeId = session?.workspaceId
  const [tags, setTags] = useState<Tag[] | null>(null)
  const [name, setName] = useState('')
  const [showArchived, setShowArchived] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const { clientId, projectId } = useFocus()
  // The manager always fetches archived too (the showArchived toggle
  // filters display) so an archived tag can still be un-archived;
  // and it honours the active focus scope like every other tag list.
  const tagQuery = useMemo(
    () => ({
      include_archived: true,
      // Manager surface: keep GLOBAL (unrestricted) generic tags visible
      // even under a focus, so an unrestricted tag stays reachable here
      // to add a "Restrict to...". Filter surfaces omit this.
      manage: true,
      ...(clientId
        ? {
            for_client: clientId,
            ...(projectId ? { for_project: projectId } : {}),
          }
        : {}),
    }),
    [clientId, projectId],
  )

  const load = useCallback(async () => {
    const { data, error } = await api.GET('/tags', {
      params: { header: workspaceHeader(), query: tagQuery },
    })
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    setTags(data)
  }, [tagQuery])

  useEffect(() => {
    let active = true
    void (async () => {
      const { data } = await api.GET('/tags', {
        params: { header: workspaceHeader(), query: tagQuery },
      })
      if (active && data) setTags(data)
    })()
    return () => {
      active = false
    }
  }, [activeId, tagQuery])

  async function onCreate(e: FormEvent) {
    e.preventDefault()
    setErr(null)
    const { error } = await api.POST('/tags', {
      params: { header: workspaceHeader() },
      body: { kind: 'generic', name },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    setName('')
    await load()
  }

  return (
    <section className="card">
      <h1>{t('tagmgr.title')}</h1>
      {err && <p className="err">{err}</p>}
      <form onSubmit={(e) => void onCreate(e)} className="row">
        <input
          required
          placeholder={t('tagmgr.rename')}
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        <button type="submit">+</button>
        <label className="row">
          <input
            type="checkbox"
            checked={showArchived}
            onChange={(e) => setShowArchived(e.target.checked)}
          />
          {t('tagmgr.showArchived')}
        </label>
      </form>
      {tags === null ? (
        <p>{t('tagmgr.loading')}</p>
      ) : (
        (() => {
          const visible = tags.filter(
            (tg) => showArchived || tg.status !== 'archived',
          )
          const targets = tags.filter(
            (tg) => tg.kind === 'project' || tg.kind === 'client',
          )
          return visible.length === 0 ? (
            <p className="hint">{t('tagmgr.none')}</p>
          ) : (
            <ul className="list">
              {visible.map((tg) =>
                tg.kind === 'generic' ? (
                  <TagRow
                    key={tg.id}
                    tag={tg}
                    targets={targets}
                    onChanged={() => void load()}
                  />
                ) : (
                  // Client/project tags are auto-created from their
                  // profile; they are managed in Clients & projects,
                  // not here. Read-only, just surface the kind.
                  <li key={tg.id}>
                    <TagChip
                      name={tg.name}
                      color={tg.color || '#4a6b3e'}
                      kind={tg.kind}
                    />
                    <span className="muted">
                      {' '}
                      {tg.kind}
                      {tg.status === 'archived'
                        ? ` · ${t('tagmgr.archived')}`
                        : ''}
                      {' · '}
                      {t('cp.managedHere')}
                    </span>{' '}
                    <Link to="/clients" className="btn--ghost btn--sm">
                      {t('cp.nav')}
                    </Link>
                  </li>
                ),
              )}
            </ul>
          )
        })()
      )}
    </section>
  )
}
