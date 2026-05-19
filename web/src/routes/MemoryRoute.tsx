import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import { useSession } from '../auth/useSession'
import { TagChip } from '../components/TagChip'
import type { components } from '../api/schema'

type Hit = components['schemas']['MemoryHitOut']
type Tag = components['schemas']['TagOut']

// Memory is hard-isolated within (workspace, project) and metered.
// "Channels" are memory-only tags (kind=memory_channel) that organise
// snippets and narrow recall; generic tags are an extra facet. Recall
// is hybrid (semantic + keyword); if no embedding model is installed
// it transparently degrades to keyword-only — snippets are still
// saved and found by words.
export function MemoryRoute() {
  const { t } = useTranslation()
  const session = useSession()
  const activeId = session?.workspaceId
  const [text, setText] = useState('')
  const [query, setQuery] = useState('')
  const [ran, setRan] = useState<string | null>(null)
  const [hits, setHits] = useState<Hit[] | null>(null)
  const [tags, setTags] = useState<Tag[]>([])
  const [channels, setChannels] = useState<Tag[]>([])
  const [sem, setSem] = useState<boolean | null>(null)
  const [wCh, setWCh] = useState('')
  const [fCh, setFCh] = useState('')
  const [wTags, setWTags] = useState<string[]>([])
  const [fTags, setFTags] = useState<string[]>([])
  const [newCh, setNewCh] = useState('')
  const [sKind, setSKind] = useState('')
  const [sId, setSId] = useState('')
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)

  const loadTags = useCallback(async () => {
    const { data } = await api.GET('/tags', {
      params: { header: workspaceHeader() },
    })
    if (data) {
      setTags(data.filter((g) => g.kind === 'generic'))
      setChannels(data.filter((g) => g.kind === 'memory_channel'))
    }
  }, [])

  useEffect(() => {
    let active = true
    void (async () => {
      const [tg, st] = await Promise.all([
        api.GET('/tags', { params: { header: workspaceHeader() } }),
        api.GET('/memory/status', {
          params: { header: workspaceHeader() },
        }),
      ])
      if (!active) return
      if (tg.data) {
        setTags(tg.data.filter((g) => g.kind === 'generic'))
        setChannels(tg.data.filter((g) => g.kind === 'memory_channel'))
      }
      setSem(st.data ? st.data.semantic : null)
    })()
    return () => {
      active = false
    }
  }, [activeId])

  function toggle(list: string[], set: (v: string[]) => void, id: string) {
    set(list.includes(id) ? list.filter((x) => x !== id) : [...list, id])
  }

  async function createChannel() {
    const name = newCh.trim()
    if (!name) return
    setErr(null)
    const { data, error } = await api.POST('/tags', {
      params: { header: workspaceHeader() },
      body: { kind: 'memory_channel', name },
    })
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    setNewCh('')
    await loadTags()
    setWCh(data.id)
  }

  const runSearch = useCallback(
    async (q: string) => {
      setErr(null)
      const { data, error } = await api.POST('/memory/search', {
        params: { header: workspaceHeader() },
        body: {
          query: q,
          operation_id: crypto.randomUUID(),
          limit: 10,
          channel_tag_id: fCh || undefined,
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
    [fCh, fTags],
  )

  async function onWrite(e: FormEvent) {
    e.preventDefault()
    setErr(null)
    setMsg(null)
    const { error } = await api.POST('/memory/blobs', {
      params: { header: workspaceHeader() },
      body: {
        text,
        // Server-side default; not user-facing (channels organise
        // memory). Sent because the generated type requires it.
        namespace: 'note',
        operation_id: crypto.randomUUID(),
        importance: 0,
        channel_tag_id: wCh || undefined,
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

  const chanSelect = (
    value: string,
    set: (v: string) => void,
    withNone: boolean,
  ) => (
    <select value={value} onChange={(e) => set(e.target.value)}>
      {withNone && <option value="">{t('memory.ch.none')}</option>}
      {channels.map((c) => (
        <option key={c.id} value={c.id}>
          {c.name}
        </option>
      ))}
    </select>
  )

  return (
    <>
      <h1 className="page-title">{t('memory.title')}</h1>

      <section className="card">
        <p className="hint">{t('memory.intro')}</p>
        {sem === false && (
          <p className="banner">{t('memory.ch.semanticOff')}</p>
        )}
        {sem === true && (
          <p className="hint ok">{t('memory.ch.semanticOn')}</p>
        )}
        {err && <p className="err">{err}</p>}
        {msg && <p className="ok">{msg}</p>}

        <h2>{t('memory.writeTitle')}</h2>
        <form onSubmit={(e) => void onWrite(e)}>
          <label>
            {t('memory.writeText')}
            <textarea
              required
              placeholder={t('memory.writePlaceholder')}
              value={text}
              onChange={(e) => setText(e.target.value)}
            />
          </label>
          <div className="row">
            <label>
              {t('memory.ch.channel')}
              {chanSelect(wCh, setWCh, true)}
            </label>
            <label>
              {t('memory.ch.create')}
              <span className="row">
                <input
                  placeholder={t('memory.ch.newName')}
                  value={newCh}
                  onChange={(e) => setNewCh(e.target.value)}
                />
                <button
                  type="button"
                  className="btn--sm btn--ghost"
                  disabled={!newCh.trim()}
                  onClick={() => void createChannel()}
                >
                  +
                </button>
              </span>
            </label>
          </div>
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
          <div>
            <button type="submit">{t('memory.write')}</button>
          </div>
        </form>
      </section>

      <section className="card">
        <h2>{t('memory.searchTitle')}</h2>
        <p className="hint">{t('memory.searchHint')}</p>
        <form
          onSubmit={(e) => {
            e.preventDefault()
            void runSearch(query)
          }}
        >
          <div className="row">
            <input
              required
              placeholder={t('memory.query')}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
            <label>
              {t('memory.ch.channel')}
              {chanSelect(fCh, setFCh, true)}
            </label>
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
            <h3>{t('memory.results')}</h3>
            {hits.length === 0 ? (
              <p className="hint">{t('memory.none')}</p>
            ) : (
              <ul className="list">
                {hits.map((h) => {
                  const blobTags = h.blob.tags ?? []
                  const own = new Set(blobTags.map((g) => g.id))
                  const indexed =
                    !!h.blob.model_id && h.blob.model_id !== 'none'
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
                          {' · '}
                          {t('memory.tier')} {h.blob.tier}
                          {' · '}
                          {t('memory.ch.score')} {h.rrf.toFixed(4)}
                          {' · '}
                          <span className="tag tag--muted">
                            {indexed
                              ? t('memory.ch.indexed')
                              : t('memory.ch.keywordOnly')}
                          </span>
                        </span>
                      </div>
                    </li>
                  )
                })}
              </ul>
            )}
          </>
        )}
      </section>

      <section className="card">
        <h2>{t('memory.eraseTitle')}</h2>
        <p className="hint">{t('memory.eraseHint')}</p>
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
    </>
  )
}
