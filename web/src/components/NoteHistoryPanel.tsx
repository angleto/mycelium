import { useCallback, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'

import { api, workspaceHeader } from '../api/client'
import type { components } from '../shared'

// ADR-0075 decision 5: a note tells what happened to it. The garden moves a
// note on its own (the maturity sweep, the dormancy of a superseded note, the
// humus mark a reviewed distillation sets) and the person moves it too; until
// this, the state changed and nothing said why. Each row says, in words, what
// happened, who did it (you, an agent, the garden) and when; the codes stay
// behind the words. Collapsed by default: it is the answer to "why", not the
// first thing on the page.

type Event = components['schemas']['NoteHistoryEventOut']

/** The actions this panel knows how to say; anything else is an edit. */
const KNOWN = new Set([
  'create',
  'update',
  'set_maturity',
  'auto_maturity',
  'auto_dormant',
  'garden_review:source_humus',
  'archive',
  'restore',
  'trash',
  'delete',
  'protect',
  'promote',
  'merge_in',
])

function who(actorKind: string): 'person' | 'agent' | 'garden' {
  if (actorKind.startsWith('human_')) return 'person'
  if (actorKind === 'system') return 'garden'
  return 'agent'
}

export function NoteHistoryPanel({ noteId }: { noteId: string }) {
  const { t, i18n } = useTranslation()
  const [items, setItems] = useState<Event[]>([])
  const [next, setNext] = useState<string | null>(null)
  const [loaded, setLoaded] = useState(false)
  // Set when the first read starts, not when it ends: a second toggle event
  // arriving before the answer must not read again.
  const requested = useRef(false)

  const load = useCallback(
    async (cursor: string | null) => {
      const { data } = await api.GET('/garden/notes/{note_id}/history', {
        params: {
          header: workspaceHeader(),
          path: { note_id: noteId },
          query: { limit: 30, ...(cursor ? { cursor } : {}) },
        },
      })
      setItems((xs) => (cursor ? [...xs, ...(data?.items ?? [])] : (data?.items ?? [])))
      setNext(data?.next_cursor ?? null)
      setLoaded(true)
    },
    [noteId],
  )


  const when = (iso: string) =>
    new Date(iso).toLocaleString(i18n.language, {
      day: 'numeric',
      month: 'short',
      year: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    })

  function say(e: Event): string {
    const key = KNOWN.has(e.action) ? e.action.replace(':', '_') : 'other'
    const m = e.changes.maturity
    const h = e.changes.humus_flag
    return t(`noteHistory.action.${key}`, {
      from: m ? t(`garden.maturity.${String(m.old)}`, { defaultValue: String(m.old) }) : '',
      to: m ? t(`garden.maturity.${String(m.new)}`, { defaultValue: String(m.new) }) : '',
      humus: h ? t(h.new ? 'noteHistory.humusOn' : 'noteHistory.humusOff') : '',
    })
  }

  return (
    <details
      className="notehistory"
      onToggle={(e) => {
        const isOpen = (e.currentTarget as HTMLDetailsElement).open
        // Read on first opening only; the parent keys this panel by note,
        // so another note starts from a fresh, unread panel.
        if (isOpen && !requested.current) {
          requested.current = true
          void load(null)
        }
      }}
    >
      <summary>{t('noteHistory.title')}</summary>
      {!loaded ? (
        <p className="hint">{t('common.loading')}</p>
      ) : items.length === 0 ? (
        <p className="hint">{t('noteHistory.empty')}</p>
      ) : (
        <ol className="notehistory__list">
          {items.map((e) => (
            <li key={e.event_id} className="notehistory__item">
              <span className="muted notehistory__when">{when(e.at)}</span>
              <span className={`chip notehistory__who notehistory__who--${who(e.actor_kind)}`}>
                {t(`noteHistory.who.${who(e.actor_kind)}`)}
              </span>
              <span>
                {say(e)}
                {e.about_note_id && (
                  <>
                    {' '}
                    <Link to={`/garden/review/${e.about_note_id}`}>
                      {t('noteHistory.openDistillation')}
                    </Link>
                  </>
                )}
              </span>
            </li>
          ))}
        </ol>
      )}
      {next && (
        <button type="button" className="btn--ghost btn--sm" onClick={() => void load(next)}>
          {t('proposals.more')}
        </button>
      )}
    </details>
  )
}
