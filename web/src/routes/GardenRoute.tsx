/**
 * Garden route (docs/adr/0029 P2).
 *
 * Three tabs over the maturity lifecycle: Inbox (seed) / Garden
 * (growing + mature) / Cemetery (dormant + transplanted). Each tab
 * lists notes with quick gardening actions: change maturity, promote
 * to task (transplant), derive a fruit task, expand to plant view
 * with backlinks + atomic children + fruit tasks.
 *
 * Pure client-side filtering: a single GET /notes load drives the
 * three tabs (notes are few-per-workspace, no pagination yet).
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'

import { api, errMessage, workspaceHeader } from '../api/client'
import { MarkdownView } from '../components/Markdown'
import type { components } from '../api/schema'

type Note = components['schemas']['NoteOut']
type NoteWithLinks = components['schemas']['NoteWithLinksOut']

type Tab = 'inbox' | 'garden' | 'cemetery'

const MATURITY_OPTIONS = ['seed', 'growing', 'mature', 'dormant'] as const

// Garden glyphs: the maturity stage is the row's identity, so render
// it as a single emoji + accessible label rather than a chip + text.
// (The keys map to garden.maturity.* in the i18n catalog.)
const MATURITY_GLYPH: Record<string, string> = {
  seed: '🌱',
  growing: '🌿',
  mature: '🌳',
  dormant: '🍂',
}

function bucketOf(n: Note): Tab {
  if (n.promoted_at) return 'cemetery'
  if (n.maturity === 'seed') return 'inbox'
  if (n.maturity === 'dormant') return 'cemetery'
  return 'garden'
}

function shortPreview(n: Note): string {
  const body = (n.transcript || n.summary || '').trim()
  if (!body) return ''
  const first = body.split('\n').find((l) => l.trim().length > 0) || ''
  return first.length > 220 ? first.slice(0, 219) + '…' : first
}

const TAB_GLYPH: Record<Tab, string> = {
  inbox: '🌱',
  garden: '🌿',
  cemetery: '🍂',
}

export function GardenRoute() {
  const { t } = useTranslation()
  const [notes, setNotes] = useState<Note[]>([])
  const [tab, setTab] = useState<Tab>('inbox')
  const [openId, setOpenId] = useState<string | null>(null)
  const [openData, setOpenData] = useState<NoteWithLinks | null>(null)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)

  const reload = useCallback(async () => {
    const { data, error } = await api.GET('/notes', {
      params: { header: workspaceHeader() },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    setNotes(data ?? [])
  }, [])

  useEffect(() => {
    let active = true
    void (async () => {
      const { data, error } = await api.GET('/notes', {
        params: { header: workspaceHeader() },
      })
      if (!active) return
      if (error) setErr(errMessage(error))
      else setNotes(data ?? [])
    })()
    return () => {
      active = false
    }
  }, [])

  const buckets = useMemo(() => {
    const out: Record<Tab, Note[]> = { inbox: [], garden: [], cemetery: [] }
    for (const n of notes) {
      if (n.deleted_at || n.is_archived) continue
      out[bucketOf(n)].push(n)
    }
    return out
  }, [notes])

  const titleOf = (n: Note) =>
    (n.title && n.title.trim()) || shortPreview(n).slice(0, 80) || n.id.slice(0, 8)

  async function setMaturity(noteId: string, maturity: string) {
    setBusy(true)
    setErr('')
    const { error } = await api.POST('/notes/{note_id}/maturity', {
      params: { header: workspaceHeader(), path: { note_id: noteId } },
      body: { maturity },
    })
    setBusy(false)
    if (error) {
      setErr(errMessage(error))
      return
    }
    await reload()
  }

  async function onPromote(noteId: string) {
    if (!window.confirm(t('garden.promoteConfirm'))) return
    setBusy(true)
    setErr('')
    const { data, error } = await api.POST('/notes/{note_id}/promote', {
      params: { header: workspaceHeader(), path: { note_id: noteId } },
      body: { title: null },
    })
    setBusy(false)
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    await reload()
    window.location.href = `/tasks/${data.task_id}`
  }

  async function onDerive(noteId: string) {
    const title = window.prompt(t('garden.derivePrompt'))
    if (!title || !title.trim()) return
    setBusy(true)
    setErr('')
    const { data, error } = await api.POST('/notes/{note_id}/derive-task', {
      params: { header: workspaceHeader(), path: { note_id: noteId } },
      body: { title: title.trim(), description: null, estimate_effort_h: null },
    })
    setBusy(false)
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    await reload()
    window.location.href = `/tasks/${data.task_id}`
  }

  const openPlant = useCallback(async (noteId: string) => {
    setOpenId(noteId)
    setOpenData(null)
    const { data, error } = await api.GET('/notes/{note_id}/links', {
      params: { header: workspaceHeader(), path: { note_id: noteId } },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    setOpenData(data ?? null)
  }, [])

  const closePlant = () => {
    setOpenId(null)
    setOpenData(null)
  }

  // Esc closes the modal — same convention as /notes.
  useEffect(() => {
    if (!openId) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') closePlant()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [openId])

  const tabs: { id: Tab; label: string }[] = [
    { id: 'inbox', label: t('garden.tab.inbox') },
    { id: 'garden', label: t('garden.tab.garden') },
    { id: 'cemetery', label: t('garden.tab.cemetery') },
  ]
  const visible = buckets[tab]

  return (
    <section className="card garden">
      <h1>{t('garden.title')}</h1>
      <p className="hint">{t('garden.intro')}</p>

      <div className="garden__tabs" role="tablist">
        {tabs.map((x) => (
          <button
            key={x.id}
            type="button"
            role="tab"
            aria-selected={tab === x.id}
            className={
              'garden__tab' + (tab === x.id ? ' garden__tab--active' : '')
            }
            onClick={() => setTab(x.id)}
          >
            <span aria-hidden="true">{TAB_GLYPH[x.id]}</span>
            <span className="garden__tab-label">{x.label}</span>
            <span className="garden__tab-count">{buckets[x.id].length}</span>
          </button>
        ))}
      </div>

      {err && <p className="err">{err}</p>}

      {visible.length === 0 ? (
        <p className="hint garden__empty">{t(`garden.empty.${tab}`)}</p>
      ) : (
        <ul className="garden__list">
          {visible.map((n) => (
            <li key={n.id} className="plant">
              <button
                type="button"
                className="plant__open"
                onClick={() => void openPlant(n.id)}
                title={t('garden.openPlant')}
              >
                <span
                  className={`plant__glyph plant__glyph--${n.maturity}`}
                  aria-hidden="true"
                >
                  {MATURITY_GLYPH[n.maturity] ?? '🌱'}
                </span>
                <span className="plant__body">
                  <span className="plant__title">{titleOf(n)}</span>
                  {shortPreview(n) && (
                    <span className="plant__preview">{shortPreview(n)}</span>
                  )}
                  {n.promoted_at && (
                    <span className="plant__chip plant__chip--promoted">
                      {t('garden.promotedChip')}
                    </span>
                  )}
                </span>
              </button>
              <div className="plant__actions">
                {!n.promoted_at && (
                  <>
                    <select
                      className="plant__maturity"
                      value={n.maturity}
                      disabled={busy}
                      aria-label={t('garden.changeMaturity')}
                      title={t('garden.changeMaturity')}
                      onChange={(e) => void setMaturity(n.id, e.target.value)}
                    >
                      {MATURITY_OPTIONS.map((m) => (
                        <option key={m} value={m}>
                          {MATURITY_GLYPH[m]} {t(`garden.maturity.${m}`)}
                        </option>
                      ))}
                    </select>
                    <button
                      type="button"
                      className="plant__btn"
                      disabled={busy}
                      title={t('garden.promote')}
                      aria-label={t('garden.promote')}
                      onClick={() => void onPromote(n.id)}
                    >
                      <span aria-hidden="true">🌸</span>
                      <span className="plant__btn-label">
                        {t('garden.promote')}
                      </span>
                    </button>
                    <button
                      type="button"
                      className="plant__btn plant__btn--ghost"
                      disabled={busy}
                      title={t('garden.derive')}
                      aria-label={t('garden.derive')}
                      onClick={() => void onDerive(n.id)}
                    >
                      <span aria-hidden="true">🍎</span>
                      <span className="plant__btn-label">
                        {t('garden.derive')}
                      </span>
                    </button>
                  </>
                )}
                <Link
                  to={`/notes?open=${n.id}`}
                  className="plant__btn plant__btn--ghost"
                  title={t('garden.openNote')}
                  aria-label={t('garden.openNote')}
                >
                  <span aria-hidden="true">↗</span>
                  <span className="plant__btn-label">
                    {t('garden.openNote')}
                  </span>
                </Link>
              </div>
            </li>
          ))}
        </ul>
      )}

      {openId && (
        <div
          className="modal__backdrop"
          role="dialog"
          aria-modal="true"
          aria-label={t('garden.title')}
          onClick={(e) => {
            if (e.target === e.currentTarget) closePlant()
          }}
        >
          <div className="modal__panel">
            <div className="modal__head">
              <strong>
                {openData
                  ? openData.note.title || t('notes.untitled')
                  : t('garden.loading')}
              </strong>
              {openData && (
                <span
                  className={`plant__glyph plant__glyph--${openData.note.maturity}`}
                  aria-hidden="true"
                >
                  {MATURITY_GLYPH[openData.note.maturity] ?? '🌱'}
                </span>
              )}
              <span className="modal__sp" />
              <button
                type="button"
                className="btn--ghost btn--sm"
                onClick={closePlant}
              >
                {t('notes.close')}
              </button>
            </div>
            <div className="modal__body">
              {openData === null ? (
                <p className="hint">{t('garden.loading')}</p>
              ) : (
                <PlantDetail
                  data={openData}
                  allNotes={notes}
                  onUnlink={async (childId, kind) => {
                    await api.DELETE('/notes/{note_id}/links', {
                      params: {
                        header: workspaceHeader(),
                        path: { note_id: openId },
                        query: { child_note_id: childId, kind },
                      },
                    })
                    await openPlant(openId)
                  }}
                />
              )}
            </div>
          </div>
        </div>
      )}
    </section>
  )
}

function PlantDetail({
  data,
  allNotes,
  onUnlink,
}: {
  data: NoteWithLinks
  allNotes: Note[]
  onUnlink: (childNoteId: string, kind: string) => Promise<void>
}) {
  const { t } = useTranslation()
  const titleById = (id: string) =>
    allNotes.find((x) => x.id === id)?.title?.trim() || id.slice(0, 8)
  const n = data.note
  return (
    <div className="plant-detail">
      <div className="plant-detail__chips">
        <span className={`chip chip--maturity chip--${n.maturity}`}>
          <span aria-hidden="true">{MATURITY_GLYPH[n.maturity] ?? '🌱'}</span>{' '}
          {t(`garden.maturity.${n.maturity}`)}
        </span>
        {n.promoted_at && (
          <span className="chip chip--promoted">{t('garden.promotedChip')}</span>
        )}
      </div>
      {n.transcript && (
        <div className="plant-detail__body md">
          <MarkdownView text={n.transcript} />
        </div>
      )}
      <section className="plant-detail__section">
        <h3>{t('garden.outgoing')}</h3>
        {data.outgoing.length === 0 ? (
          <p className="hint">{t('garden.none')}</p>
        ) : (
          <ul className="plant-detail__links">
            {data.outgoing.map((l) => (
              <li key={l.id}>
                <span className="chip chip--linkkind">{l.kind}</span>{' '}
                <Link to={`/notes?open=${l.child_note_id}`}>
                  {titleById(l.child_note_id)}
                </Link>{' '}
                <button
                  type="button"
                  className="btn--ghost btn--sm"
                  onClick={() => void onUnlink(l.child_note_id, l.kind)}
                >
                  {t('garden.unlink')}
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>
      <section className="plant-detail__section">
        <h3>{t('garden.incoming')}</h3>
        {data.incoming.length === 0 ? (
          <p className="hint">{t('garden.none')}</p>
        ) : (
          <ul className="plant-detail__links">
            {data.incoming.map((l) => (
              <li key={l.id}>
                <span className="chip chip--linkkind">{l.kind}</span>{' '}
                <Link to={`/notes?open=${l.parent_note_id}`}>
                  {titleById(l.parent_note_id)}
                </Link>
              </li>
            ))}
          </ul>
        )}
      </section>
      <section className="plant-detail__section">
        <h3>{t('garden.fruits')}</h3>
        {data.task_links.length === 0 ? (
          <p className="hint">{t('garden.none')}</p>
        ) : (
          <ul className="plant-detail__links">
            {data.task_links.map((l) => (
              <li key={l.id}>
                <span className="chip chip--linkkind">{l.kind}</span>{' '}
                <Link to={`/tasks/${l.task_id}`}>
                  {l.task_id.slice(0, 8)}
                </Link>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  )
}
