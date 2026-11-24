import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'

import { api, workspaceHeader } from '../api/client'
import type { components } from '../shared'

// ADR-0075 decision 5: a note tells what happened to it. What the garden
// generated from this note (a distillation, a pattern, a season) is held out
// of the note's links and of every listing until a person approves it, so
// without this the source gave no sign that a proposal about it was waiting,
// had been declined, or had been approved. One row per generated note, its
// state in words, and the way to the reading view where it is decided.
//
// Renders nothing while loading, on an error, and when nothing was generated:
// a note with no proposals has nothing to say here, and an error on a
// secondary panel must not cover the note.

type Item = components['schemas']['GardenReviewForSourceItem']

export function GeneratedFromNotePanel({ noteId }: { noteId: string }) {
  const { t, i18n } = useTranslation()
  const [items, setItems] = useState<Item[]>([])

  useEffect(() => {
    let active = true
    void (async () => {
      const { data } = await api.GET('/garden/review/by-source/{note_id}', {
        params: { header: workspaceHeader(), path: { note_id: noteId } },
      })
      if (active) setItems(data ?? [])
    })()
    return () => {
      active = false
    }
  }, [noteId])

  if (items.length === 0) return null

  const when = (iso: string | null | undefined) =>
    iso ? new Date(iso).toLocaleDateString(i18n.language, { day: 'numeric', month: 'short' }) : ''

  return (
    <section className="generated" aria-labelledby={`generated-${noteId}`}>
      <h2 id={`generated-${noteId}`} className="generated__title">
        {t('generatedFromNote.title')}
      </h2>
      <ul className="generated__list">
        {items.map((x) => (
          <li key={x.note_id} className="generated__item">
            <span className={`chip gproposal__state gproposal__state--${x.review_state}`}>
              {t(`gardenProposal.state.${x.review_state}`)}
            </span>
            <span className="generated__name">
              {t(`gardenProposal.kind.${x.humus_kind ?? 'distillation'}`, {
                defaultValue: x.humus_kind ?? '',
              })}
              {x.decided_at ? ` · ${when(x.decided_at)}` : ` · ${when(x.created_at)}`}
            </span>
            <Link to={`/garden/review/${x.note_id}`} className="btn--ghost btn--sm">
              {x.review_state === 'proposed'
                ? t('generatedFromNote.readAndDecide')
                : t('generatedFromNote.read')}
            </Link>
          </li>
        ))}
      </ul>
    </section>
  )
}
