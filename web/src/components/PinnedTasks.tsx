import { useTranslation } from 'react-i18next'
import { usePins } from '../api/pins'
import { TaskShortRow } from './RecentTasks'
import type { components } from '../shared'

type Task = components['schemas']['TaskOut']

// The tasks the person has pinned (migration 0027), above the recent ones:
// the way back to what they are working on, one tap from the top of /tasks
// on every device.
//
// Nothing at all when there is nothing pinned: an empty box with a heading
// would be one more thing between the person and the list, for a feature
// they have not used yet. Not collapsible either: a pin IS the request to
// keep a task in sight, and a closed panel would undo it.
//
// Every pin is shown, whatever the page's filters and focus are, closed and
// archived tasks included: the person pinned it, so it stays until they
// take it off, and the pin control on each row is how they do that. A row
// the page has already loaded is drawn from the page's copy, which is the
// fresher one (a state just changed on the board shows here at once); the
// rest come from the pinned list itself.
export function PinnedTasks({ tasks }: { tasks: Task[] }) {
  const { t } = useTranslation()
  const { ids, tasks: pinned } = usePins()
  if (ids.length === 0) return null
  const fresh = new Map(tasks.map((x) => [x.id, x]))
  const stored = new Map(pinned.map((x) => [x.id, x]))
  const rows = ids
    .map((id) => fresh.get(id) ?? stored.get(id))
    .filter((x): x is Task => x !== undefined)
  if (rows.length === 0) return null
  return (
    <section className="recentwidget pinnedwidget" aria-labelledby="pinnedwidget-title">
      <div className="recentwidget__head">
        <h2 id="pinnedwidget-title" className="pinnedwidget__title">
          <span aria-hidden="true">📌</span> {t('tasks.pinnedTitle')}
        </h2>
      </div>
      <ul className="list recentlist">
        {rows.map((tk) => (
          <TaskShortRow key={tk.id} task={tk} />
        ))}
      </ul>
    </section>
  )
}
