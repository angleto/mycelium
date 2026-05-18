import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import { useSession } from '../auth/useSession'
import { TagChip } from '../components/TagChip'
import type { components } from '../api/schema'

type Hit = components['schemas']['MemoryHitOut']
type Tag = components['schemas']['TagOut']

// Memory is hard-isolated within (workspace, project) and metered.
// Tags are an orthogonal facet inside that boundary: they narrow
// retrieval, never cross it. operation_id makes write/search
// idempotent on retry.
export function MemoryRoute() {
  const { t } = useTranslation()
  const session = useSession()
  const activeId = session?.workspaceId
  const [text, setText] = useState('')
  const [ns, setNs] = useState('note')
  const [query, setQuery] = useState('')
  const [ran, setRan] = useState<string | null>(null)
  const [hits, setHits] = useState<Hit[] | null>(null)
  const [tags, setTags] = useState<Tag[]>([])
  const [wTags, setWTags] = useState<string[]>([])
  const [fTags, setFTags] = useState<string[]>([])
  const [sKind, setSKind] = useState('')
  const [sId, setSId] = useState('')
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)

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

  function toggle(list: string[], set: (v: string[]) => void, id: string) {
    set(list.includes(id) ? list.filter((x) => x !== id) : [...list, id])
  }

  const runSearch = useCallback(
    async (q: string) => {
      const { data, error } = await api.POST('/memory/search', {
        params: { header: workspaceHeader() },
        body: {
          query: q,
          operation_id: crypto.randomUUID(),
          limit: 10,
          tag_ids: fTags,
        },
      })
      if (error || !data) {
        setErr(errMessage(error))
        return
      }
      setRan(q)
      setHits(data)
    },
    [fTags],
  )

  async function onWrite(e: FormEvent) {
    e.preventDefault()
    setErr(null)
    setMsg(null)
    const { error } = await api.POST('/memory/blobs', {
      params: { header: workspaceHeader() },
      body: {
        text,
        namespace: ns,
        operation_id: crypto.randomUUID(),
        importance: 0,
        tag_ids: wTags,
      },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    setText('')
    setWTags([])
    setMsg(t('memory.write'))
  }

  async function onSearch(e: FormEvent) {
    e.preventDefault()
    setErr(null)
    await runSearch(query)
  }

  async function attach(blobId: string, tagId: string) {
    setErr(null)
    const { error } = await api.POST('/memory/blobs/{blob_id}/tags', {
      params: { header: workspaceHeader(), path: { blob_id: blobId } },
      body: { tag_id: tagId },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    if (ran !== null) await runSearch(ran)
  }

  async function detach(blobId: string, tagId: string) {
    setErr(null)
    const { error } = await api.DELETE('/memory/blobs/{blob_id}/tags/{tag_id}', {
      params: {
        header: workspaceHeader(),
        path: { blob_id: blobId, tag_id: tagId },
      },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    if (ran !== null) await runSearch(ran)
  }

  async function onErase(e: FormEvent) {
    e.preventDefault()
    setErr(null)
    setMsg(null)
    const { data, error } = await api.POST('/memory/erase', {
      params: { header: workspaceHeader() },
      body: { source_kind: sKind, source_id: sId },
    })
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    setMsg(t('memory.erased', { n: data.deleted }))
  }

  return (
    <section className="card">
      <h1>{t('memory.title')}</h1>
      <p className="hint">{t('memory.meteredNote')}</p>
      {err && <p className="err">{err}</p>}
      {msg && <p className="ok">{msg}</p>}

      <form onSubmit={(e) => void onWrite(e)}>
        <label>
          {t('memory.writeText')}
          <textarea
            required
            value={text}
            onChange={(e) => setText(e.target.value)}
          />
        </label>
        {tags.length > 0 && (
          <div className="row">
            <span className="muted">{t('memory.tags')}:</span>
            {tags.map((g) => (
              <button
                key={g.id}
                type="button"
                className={
                  'btn--sm' + (wTags.includes(g.id) ? '' : ' btn--ghost')
                }
                onClick={() => toggle(wTags, setWTags, g.id)}
              >
                <TagChip name={g.name} color={g.color} kind={g.kind} />
              </button>
            ))}
          </div>
        )}
        <div className="row">
          <label>
            {t('memory.namespace')}
            <input value={ns} onChange={(e) => setNs(e.target.value)} />
          </label>
          <button type="submit">{t('memory.write')}</button>
        </div>
      </form>

      <form onSubmit={(e) => void onSearch(e)}>
        <div className="row">
          <input
            required
            placeholder={t('memory.query')}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <button type="submit">{t('memory.search')}</button>
        </div>
        {tags.length > 0 && (
          <div className="row">
            <span className="muted">{t('memory.filterTags')}:</span>
            {tags.map((g) => (
              <button
                key={g.id}
                type="button"
                className={
                  'btn--sm' + (fTags.includes(g.id) ? '' : ' btn--ghost')
                }
                onClick={() => toggle(fTags, setFTags, g.id)}
              >
                <TagChip name={g.name} color={g.color} kind={g.kind} />
              </button>
            ))}
          </div>
        )}
      </form>

      {hits && (
        <>
          <h2>{t('memory.results')}</h2>
          {hits.length === 0 ? (
            <p className="hint">{t('memory.none')}</p>
          ) : (
            <ul className="list">
              {hits.map((h) => {
                const blobTags = h.blob.tags ?? []
                const own = new Set(blobTags.map((g) => g.id))
                return (
                  <li key={h.blob.id}>
                    <div>{h.blob.text ?? ''}</div>
                    <div className="row">
                      {blobTags.map((g) => (
                        <button
                          key={g.id}
                          type="button"
                          className="btn--sm btn--ghost"
                          title={t('graph.remove')}
                          onClick={() => void detach(h.blob.id, g.id)}
                        >
                          <TagChip
                            name={g.name}
                            color={g.color}
                            kind={g.kind}
                          />{' '}
                          ✕
                        </button>
                      ))}
                      <select
                        value=""
                        aria-label={t('memory.addTag')}
                        onChange={(e) =>
                          e.target.value &&
                          void attach(h.blob.id, e.target.value)
                        }
                      >
                        <option value="">+ {t('memory.addTag')}</option>
                        {tags
                          .filter((g) => !own.has(g.id))
                          .map((g) => (
                            <option key={g.id} value={g.id}>
                              {g.name}
                            </option>
                          ))}
                      </select>
                      <span className="muted">
                        · {t('memory.tier')} {h.blob.tier} · rrf{' '}
                        {h.rrf.toFixed(4)}
                      </span>
                    </div>
                  </li>
                )
              })}
            </ul>
          )}
        </>
      )}

      <h2>{t('memory.eraseTitle')}</h2>
      <form onSubmit={(e) => void onErase(e)} className="row">
        <input
          required
          placeholder={t('memory.sourceKind')}
          value={sKind}
          onChange={(e) => setSKind(e.target.value)}
        />
        <input
          required
          placeholder={t('memory.sourceId')}
          value={sId}
          onChange={(e) => setSId(e.target.value)}
        />
        <button type="submit">{t('memory.erase')}</button>
      </form>
    </section>
  )
}
