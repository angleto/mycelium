import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { pinStore, type PinKind } from '../api/pins'

// Pin a task or a note above the recent ones, or take it off (migrations
// 0027 and 0031).
//
// A toggle with a stable name and `aria-pressed` for its state, like the
// focus toggle beside it: a screen reader hears "Pin, pressed" rather than
// a label that flips between two verbs. It is on every row that lists the
// item, the pinned list included, because that list is where a pin is most
// often taken off, and it works on a closed task or an archived note the
// same as on an open one: finishing with something is not the moment the
// person stops wanting it at hand, unpinning it is.
export function PinButton({ kind, id }: { kind: PinKind; id: string }) {
  const { t } = useTranslation()
  const store = pinStore(kind)
  const { ids, error } = store.useStore()
  const [busy, setBusy] = useState(false)
  const on = ids.includes(id)
  // Literal keys, so the catalogue check sees every one of them.
  const label =
    kind === 'task'
      ? { name: t('tasks.pin'), pin: t('tasks.pinHint'), unpin: t('tasks.unpinHint') }
      : { name: t('notes.pin'), pin: t('notes.pinHint'), unpin: t('notes.unpinHint') }
  // A refused press (the note pin cap, a note gone to the bin meanwhile)
  // is undone by the store; the server's reason is said beside the control
  // that was pressed, or the person sees the pin flick back for no reason.
  const refused = error?.id === id ? error.message : null
  const button = (
    <button
      type="button"
      className={`btn--ghost btn--sm pinbtn${on ? ' is-on' : ''}`}
      aria-pressed={on}
      aria-label={label.name}
      title={on ? label.unpin : label.pin}
      disabled={busy}
      onClick={async () => {
        setBusy(true)
        try {
          await store.setPinned(id, !on)
        } finally {
          setBusy(false)
        }
      }}
    >
      <span aria-hidden="true">📌</span>
    </button>
  )
  if (!refused) return button
  return (
    <>
      {button}
      <span className="hint pinbtn__refused" role="alert">
        {refused}
      </span>
    </>
  )
}
