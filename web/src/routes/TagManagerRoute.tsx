import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import { useSession } from '../auth/useSession'
import { TagChip } from '../components/TagChip'
import type { components } from '../api/schema'

type Tag = components['schemas']['TagOut']

function TagRow({ tag, onChanged }: { tag: Tag; onChanged: () => void }) {
  const { t } = useTranslation()
  const [name, setName] = useState(tag.name)
  const [color, setColor] = useState(tag.color || '#6d28d9')
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const archived = tag.status === 'archived'

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
        value={/^#[0-9a-fA-F]{6}$/.test(color) ? color : '#6d28d9'}
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

  const load = useCallback(async () => {
    const { data, error } = await api.GET('/tags', {
      params: { header: workspaceHeader() },
    })
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    setTags(data)
  }, [])

  useEffect(() => {
    let active = true
    void (async () => {
      const { data } = await api.GET('/tags', {
        params: { header: workspaceHeader() },
      })
      if (active && data) setTags(data)
    })()
    return () => {
      active = false
    }
  }, [activeId])

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
          return visible.length === 0 ? (
            <p className="hint">{t('tagmgr.none')}</p>
          ) : (
            <ul className="list">
              {visible.map((tg) => (
                <TagRow key={tg.id} tag={tg} onChanged={() => void load()} />
              ))}
            </ul>
          )
        })()
      )}
    </section>
  )
}
