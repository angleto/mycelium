import { useTranslation } from 'react-i18next'
import { useNotePins } from '../api/pins'
import { NoteShortRow } from './RecentNotes'
import type { components } from '../shared'

type Note = components['schemas']['NoteListOut']

// The notes the person has pinned (migration 0033), above the recent ones:
// the way back to what they keep at hand, one tap from the top of /notes on
// every device. PinnedTasks does the same on /tasks, and the two read alike.
//
// Nothing at all when there is nothing pinned: an empty box with a heading
// would be one more thing between the person and the list, for a feature
// they have not used yet. Not collapsible either: a pin IS the request to
// keep a note in sight, and a closed panel would undo it.
//
// Every pin is shown, whatever the page's filters and focus are, archived
// notes included: the person pinned it, so it stays until they take it off,
// and the pin control on each row is how they do that. A row the page has
// already loaded is drawn from the page's copy, which is the fresher one (a
// title just renamed shows here at once); the rest come from the pinned list
// itself.
export function PinnedNotes({ notes }: { notes: Note[] }) {
  const { t } = useTranslation()
  const { ids, rows: pinned } = useNotePins()
  if (ids.length === 0) return null
  const fresh = new Map(notes.map((x) => [x.id, x]))
  const stored = new Map(pinned.map((x) => [x.id, x]))
  const rows = ids
    .map((id) => fresh.get(id) ?? stored.get(id))
    .filter((x): x is Note => x !== undefined)
  if (rows.length === 0) return null
  return (
    <section className="recentwidget pinnedwidget" aria-labelledby="pinnednotes-title">
      <div className="recentwidget__head">
        <h2 id="pinnednotes-title" className="pinnedwidget__title">
          <span aria-hidden="true">📌</span> {t('notes.pinnedTitle')}
        </h2>
      </div>
      <ul className="list recentlist">
        {rows.map((n) => (
          <NoteShortRow key={n.id} note={n} />
        ))}
      </ul>
    </section>
  )
}
