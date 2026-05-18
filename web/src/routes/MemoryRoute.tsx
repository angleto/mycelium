import { useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import type { components } from '../api/schema'

type Hit = components['schemas']['MemoryHitOut']

// Memory is hard-isolated within (workspace, project) and metered.
// operation_id makes write/search idempotent on retry.
export function MemoryRoute() {
  const { t } = useTranslation()
  const [text, setText] = useState('')
  const [ns, setNs] = useState('note')
  const [query, setQuery] = useState('')
  const [hits, setHits] = useState<Hit[] | null>(null)
  const [sKind, setSKind] = useState('')
  const [sId, setSId] = useState('')
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)

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
      },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    setText('')
    setMsg(t('memory.write'))
  }

  async function onSearch(e: FormEvent) {
    e.preventDefault()
    setErr(null)
    const { data, error } = await api.POST('/memory/search', {
      params: { header: workspaceHeader() },
      body: { query, operation_id: crypto.randomUUID(), limit: 10 },
    })
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    setHits(data)
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
        <div className="row">
          <label>
            {t('memory.namespace')}
            <input value={ns} onChange={(e) => setNs(e.target.value)} />
          </label>
          <button type="submit">{t('memory.write')}</button>
        </div>
      </form>

      <form onSubmit={(e) => void onSearch(e)} className="row">
        <input
          required
          placeholder={t('memory.query')}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <button type="submit">{t('memory.search')}</button>
      </form>

      {hits && (
        <>
          <h2>{t('memory.results')}</h2>
          {hits.length === 0 ? (
            <p className="hint">{t('memory.none')}</p>
          ) : (
            <ul className="list">
              {hits.map((h) => (
                <li key={h.blob.id}>
                  {h.blob.text ?? ''}
                  <span className="muted">
                    {' '}
                    · {t('memory.tier')} {h.blob.tier} · rrf{' '}
                    {h.rrf.toFixed(4)}
                  </span>
                </li>
              ))}
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
