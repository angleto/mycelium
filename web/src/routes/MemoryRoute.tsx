import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type FormEvent,
} from 'react'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import { useSession } from '../auth/useSession'
import { useFocus } from '../lib/focus'
import { TagChip } from '../components/TagChip'
import { TagPickerGrid } from '../components/TagPickerGrid'
import type { components } from '../api/schema'

type Hit = components['schemas']['MemoryHitOut']
type Tag = components['schemas']['TagOut']
type Channel = components['schemas']['MemoryChannelOut']

// Memory is hard-isolated within (workspace, project) and metered.
// "Channels" are a controlled, seeded vocabulary (email, telegram,
// manual, agent, ...) so integrations have a deterministic target;
// they are configured (Settings, platform admin), not created here.
// Generic tags are an extra facet. Recall is hybrid (semantic +
// keyword); with no embedding model it degrades to keyword-only —
// snippets are still saved and found by words.
export function MemoryRoute() {
  const { t } = useTranslation()
  const session = useSession()
  const activeId = session?.workspaceId
  const { clientId, projectId } = useFocus()
  // Scope the tag catalog to the active focus (server-side), so a
  // focused client/project does not surface other clients' tags.
  const tagQuery = useMemo(
    () =>
      clientId
        ? {
            for_client: clientId,
            ...(projectId ? { for_project: projectId } : {}),
          }
        : undefined,
    [clientId, projectId],
  )
  const [text, setText] = useState('')
  const [query, setQuery] = useState('')
  const [ran, setRan] = useState<string | null>(null)
  const [hits, setHits] = useState<Hit[] | null>(null)
  const [tags, setTags] = useState<Tag[]>([])
  const [channels, setChannels] = useState<Channel[]>([])
  const [sem, setSem] = useState<boolean | null>(null)
  const [wCh, setWCh] = useState('')
  const [fCh, setFCh] = useState('')
  const [wTags, setWTags] = useState<string[]>([])
  const [fTags, setFTags] = useState<string[]>([])
  const [sKind, setSKind] = useState('')
  const [sId, setSId] = useState('')
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  // Long ops (embedding cold-start, hybrid search) need a visible
  // working state; tag curation is per-blob so it doesn't freeze the
  // whole list.
  const [searching, setSearching] = useState(false)
  const [writing, setWriting] = useState(false)
  const [busyBlob, setBusyBlob] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    void (async () => {
      const [tg, st, ch] = await Promise.all([
        api.GET('/tags', {
          params: { header: workspaceHeader(), query: tagQuery },
        }),
        api.GET('/memory/status', {
          params: { header: workspaceHeader() },
        }),
        api.GET('/memory/channels', {
          params: { header: workspaceHeader() },
        }),
      ])
      if (!active) return
      // Assignable tags: any active tag except channels (channels are
      // a separate facet). Archived tags must not be offered.
      if (tg.data)
        setTags(
          tg.data.filter(
            (g) => g.kind !== 'memory_channel' && g.status === 'active',
          ),
        )
      setSem(st.data ? st.data.semantic : null)
      if (ch.data) setChannels(ch.data)
    })()
    return () => {
      active = false
    }
  }, [activeId, tagQuery])

  function toggle(list: string[], set: (v: string[]) => void, id: string) {
    set(list.includes(id) ? list.filter((x) => x !== id) : [...list, id])
  }

  const runSearch = useCallback(
    async (q: string) => {
      setErr(null)
      setSearching(true)
      try {
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
      } finally {
        setSearching(false)
      }
    },
    [fCh, fTags],
  )

  async function onWrite(e: FormEvent) {
    e.preventDefault()
    setErr(null)
    setMsg(null)
    setWriting(true)
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
    setWriting(false)
    if (error) {
      setErr(errMessage(error))
      return
    }
    setText('')
    setWTags([])
    setMsg(t('memory.saved'))
  }

  // Optimistic: the slow part was re-running the whole hybrid search
  // after every tag change. Mutate the local result instead and only
  // resync (re-search) if the server rejects it.
  function patchHitTags(
    blobId: string,
    fn: (tags: NonNullable<Hit['blob']['tags']>) => NonNullable<Hit['blob']['tags']>,
  ) {
    setHits((hs) =>
      hs
        ? hs.map((h) =>
            h.blob.id === blobId
              ? { ...h, blob: { ...h.blob, tags: fn(h.blob.tags ?? []) } }
              : h,
          )
        : hs,
    )
  }

  async function attach(blobId: string, tagId: string) {
    const g = tags.find((x) => x.id === tagId)
    if (!g) return
    setErr(null)
    setBusyBlob(blobId)
    patchHitTags(blobId, (ts) =>
      ts.some((x) => x.id === g.id)
        ? ts
        : [...ts, { id: g.id, kind: g.kind, name: g.name, color: g.color }],
    )
    const { error } = await api.POST('/memory/blobs/{blob_id}/tags', {
      params: { header: workspaceHeader(), path: { blob_id: blobId } },
      body: { tag_id: tagId },
    })
    setBusyBlob(null)
    if (error) {
      setErr(errMessage(error))
      if (ran !== null) await runSearch(ran)
    }
  }

  async function detach(blobId: string, tagId: string) {
    setErr(null)
    setBusyBlob(blobId)
    patchHitTags(blobId, (ts) => ts.filter((x) => x.id !== tagId))
    const { error } = await api.DELETE('/memory/blobs/{blob_id}/tags/{tag_id}', {
      params: {
        header: workspaceHeader(),
        path: { blob_id: blobId, tag_id: tagId },
      },
    })
    setBusyBlob(null)
    if (error) {
      setErr(errMessage(error))
      if (ran !== null) await runSearch(ran)
    }
  }

  async function deleteBlob(blobId: string) {
    if (!window.confirm(t('memory.confirmDelete'))) return
    setErr(null)
    setBusyBlob(blobId)
    const { error } = await api.DELETE('/memory/blobs/{blob_id}', {
      params: { header: workspaceHeader(), path: { blob_id: blobId } },
    })
    setBusyBlob(null)
    if (error) {
      setErr(errMessage(error))
      return
    }
    setHits((hs) => (hs ? hs.filter((h) => h.blob.id !== blobId) : hs))
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
      {channels
        .filter((c) => c.enabled)
        .map((c) => (
          <option key={c.id} value={c.id} title={c.description ?? undefined}>
            {c.name}
            {c.description ? ` — ${c.description}` : ''}
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
            {channels.filter((c) => c.enabled).length === 0 && (
              <span className="hint">{t('memory.ch.noneConfigured')}</span>
            )}
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
            <button type="submit" disabled={writing}>
              {writing ? t('memory.saving') : t('memory.write')}
            </button>
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
            <button type="submit" disabled={searching}>
              {searching ? t('memory.searching') : t('memory.search')}
            </button>
          </div>
          {tags.length > 0 && (
            <div className="row">
              <span className="muted">{t('memory.filterTags')}:</span>
              <TagPickerGrid
                tags={tags}
                selected={fTags}
                onToggle={(id) => toggle(fTags, setFTags, id)}
                searchable={false}
              />
            </div>
          )}
        </form>

        {searching && <p className="hint">{t('memory.searching')}…</p>}

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
                  const rowBusy = busyBlob === h.blob.id
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
                            disabled={rowBusy}
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
                          disabled={rowBusy}
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
                        {rowBusy && (
                          <span className="muted">{t('memory.working')}…</span>
                        )}
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
                        <button
                          type="button"
                          className="btn--sm btn--danger"
                          disabled={rowBusy}
                          onClick={() => void deleteBlob(h.blob.id)}
                        >
                          {t('memory.delete')}
                        </button>
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
        <details>
          <summary>{t('memory.eraseTitle')}</summary>
          <p className="hint">{t('memory.eraseHelp')}</p>
          <form onSubmit={(e) => void onErase(e)} className="row">
            <label>
              {t('memory.sourceKind')}
              <select
                required
                value={sKind}
                onChange={(e) => setSKind(e.target.value)}
              >
                <option value="">—</option>
                <option value="note">{t('memory.srcNote')}</option>
                <option value="task">{t('memory.srcTask')}</option>
              </select>
            </label>
            <label>
              {t('memory.sourceId')}
              <input
                required
                placeholder={t('memory.sourceIdHint')}
                value={sId}
                onChange={(e) => setSId(e.target.value)}
              />
            </label>
            <button type="submit">{t('memory.erase')}</button>
          </form>
        </details>
      </section>
    </>
  )
}
