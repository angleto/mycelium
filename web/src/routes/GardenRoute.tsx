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

const MATURITY_OPTIONS: { value: string; key: string }[] = [
  { value: 'seed', key: 'garden.maturity.seed' },
  { value: 'growing', key: 'garden.maturity.growing' },
  { value: 'mature', key: 'garden.maturity.mature' },
  { value: 'dormant', key: 'garden.maturity.dormant' },
]

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
  return first.length > 160 ? first.slice(0, 159) + '…' : first
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
    (n.title && n.title.trim()) || shortPreview(n) || n.id.slice(0, 8)

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
    // Navigate to the freshly created task.
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

  const tabs: { id: Tab; label: string }[] = [
    { id: 'inbox', label: t('garden.tab.inbox') },
    { id: 'garden', label: t('garden.tab.garden') },
    { id: 'cemetery', label: t('garden.tab.cemetery') },
  ]
  const visible = buckets[tab]

  return (
    <div className="garden">
      <h1>{t('garden.title')}</h1>
      <p className="hint">{t('garden.intro')}</p>
      <div className="tabs">
        {tabs.map((x) => (
          <button
            key={x.id}
            type="button"
            className={tab === x.id ? 'tab tab--active' : 'tab'}
            onClick={() => setTab(x.id)}
          >
            {x.label} <span className="badge">{buckets[x.id].length}</span>
          </button>
        ))}
      </div>
      {err && <p className="err">{err}</p>}
      {visible.length === 0 ? (
        <p className="hint">{t(`garden.empty.${tab}`)}</p>
      ) : (
        <ul className="plants">
          {visible.map((n) => (
            <li key={n.id} className="plant">
              <div className="plant__head">
                <button
                  type="button"
                  className="plant__title"
                  onClick={() => void openPlant(n.id)}
                >
                  {titleOf(n)}
                </button>
                <span className={`chip chip--maturity chip--${n.maturity}`}>
                  {t(`garden.maturity.${n.maturity}`)}
                </span>
                {n.promoted_at && (
                  <span className="chip chip--promoted">
                    {t('garden.promotedChip')}
                  </span>
                )}
              </div>
              <p className="plant__preview">{shortPreview(n)}</p>
              <div className="plant__actions">
                {!n.promoted_at && (
                  <>
                    <label className="plant__matpick">
                      {t('garden.changeMaturity')}
                      <select
                        value={n.maturity}
                        disabled={busy}
                        onChange={(e) => void setMaturity(n.id, e.target.value)}
                      >
                        {MATURITY_OPTIONS.map((m) => (
                          <option key={m.value} value={m.value}>
                            {t(m.key)}
                          </option>
                        ))}
                      </select>
                    </label>
                    <button
                      type="button"
                      className="btn--sm"
                      disabled={busy}
                      onClick={() => void onPromote(n.id)}
                    >
                      {t('garden.promote')}
                    </button>
                    <button
                      type="button"
                      className="btn--sm btn--ghost"
                      disabled={busy}
                      onClick={() => void onDerive(n.id)}
                    >
                      {t('garden.derive')}
                    </button>
                  </>
                )}
                <Link to={`/notes?open=${n.id}`} className="btn--sm btn--ghost">
                  {t('garden.openNote')}
                </Link>
              </div>
            </li>
          ))}
        </ul>
      )}

      {openId && (
        <div className="plant-modal" role="dialog" aria-modal="true">
          <div className="plant-modal__body">
            <button
              type="button"
              className="plant-modal__close"
              onClick={closePlant}
            >
              ✕
            </button>
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
      )}
    </div>
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
      <h2>{n.title || n.id.slice(0, 8)}</h2>
      <div className="row">
        <span className={`chip chip--maturity chip--${n.maturity}`}>
          {t(`garden.maturity.${n.maturity}`)}
        </span>
        {n.promoted_at && (
          <span className="chip chip--promoted">{t('garden.promotedChip')}</span>
        )}
      </div>
      {n.transcript && (
        <div className="plant-detail__body">
          <MarkdownView text={n.transcript} />
        </div>
      )}
      <h3>{t('garden.outgoing')}</h3>
      {data.outgoing.length === 0 ? (
        <p className="hint">{t('garden.none')}</p>
      ) : (
        <ul>
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
      <h3>{t('garden.incoming')}</h3>
      {data.incoming.length === 0 ? (
        <p className="hint">{t('garden.none')}</p>
      ) : (
        <ul>
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
      <h3>{t('garden.fruits')}</h3>
      {data.task_links.length === 0 ? (
        <p className="hint">{t('garden.none')}</p>
      ) : (
        <ul>
          {data.task_links.map((l) => (
            <li key={l.id}>
              <span className="chip chip--linkkind">{l.kind}</span>{' '}
              <Link to={`/tasks/${l.task_id}`}>{l.task_id.slice(0, 8)}</Link>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
