import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { setPinned, usePins } from '../api/pins'

// Pin a task above the recent ones, or take it off (migration 0027).
//
// A toggle with a stable name and `aria-pressed` for its state, like the
// focus toggle beside it: a screen reader hears "Pin, pressed" rather than
// a label that flips between two verbs. It is on every row that lists a
// task, the pinned list included, because that list is where a pin is most
// often taken off, and it works on a closed task the same as on an open
// one: finishing a task is not the moment the person stops wanting it at
// hand, unpinning it is.
export function PinButton({ taskId }: { taskId: string }) {
  const { t } = useTranslation()
  const { ids } = usePins()
  const [busy, setBusy] = useState(false)
  const on = ids.includes(taskId)
  return (
    <button
      type="button"
      className={`btn--ghost btn--sm pinbtn${on ? ' is-on' : ''}`}
      aria-pressed={on}
      aria-label={t('tasks.pin')}
      title={on ? t('tasks.unpinHint') : t('tasks.pinHint')}
      disabled={busy}
      onClick={async () => {
        setBusy(true)
        try {
          await setPinned(taskId, !on)
        } finally {
          setBusy(false)
        }
      }}
    >
      <span aria-hidden="true">📌</span>
    </button>
  )
}
