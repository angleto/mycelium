import { useTranslation } from 'react-i18next'
import { fmtDateTime } from '../lib/tz'
import type { Possession } from '../shared'

// Who is working on a task, said on the BOARD rather than on the card.
// The banner on the task page (TaskDetailRoute) answers the same
// question one task at a time, which means learning that a card is
// taken costs opening it -- and with ten sessions pulling from one
// column, the board is exactly where the collision is prevented instead
// of arbitrated.
//
// Three renderings and never two at once, which is the whole design: a
// row carries ONE fact, the one its reader can act on. While a card is
// held, who passed it here changes nothing anybody can do with it --
// they cannot take it; once it is free, that name is the entire
// decision, because the check is done by somebody other than whoever did
// the work. Rendering both would double the chips on a board to say
// something true and useless half the time.
//
// The hierarchy is carried by weight, not by colour alone: the live
// hold is filled and coloured because it stops an action, the lapsed one
// is outlined because it un-stops it, and the handoff is quiet text
// because it informs a choice rather than blocking one. Same slot on
// every row and on every card, so the eye finds it without reading.
// ``undefined`` from a board's map and ``null`` from the one-task
// question mean the same thing, so both are accepted rather than made
// the caller's problem to normalise.
function LockIcon({ open }: { open: boolean }) {
  return (
    <svg
      viewBox="0 0 24 24"
      width="11"
      height="11"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <rect x="4" y="11" width="16" height="9" rx="2" />
      {open ? <path d="M8 11V7a4 4 0 0 1 7.5-2" /> : <path d="M8 11V7a4 4 0 0 1 8 0v4" />}
    </svg>
  )
}

export function LeaseBadge({ possession }: { possession: Possession | null | undefined }) {
  const { t } = useTranslation()
  if (!possession) return null
  const holder = possession.lease.holder_label
  const until = fmtDateTime(possession.lease.expires_at)
  if (possession.kind === 'handoff') {
    return (
      <span
        className="leasebadge leasebadge--handoff"
        title={t('tasks.handedOffTitle', {
          holder,
          when: fmtDateTime(possession.lease.released_at ?? possession.lease.expires_at),
        })}
      >
        {t('tasks.handedOffBy', { holder })}
      </span>
    )
  }
  if (possession.kind === 'stale') {
    // The lapsed one names the holder too: "somebody was on this and
    // stopped" is a different thing to know from "nobody ever was", and
    // the name is what turns it into a question you can ask.
    const lapsed = t('tasks.heldLapsedTitle', { holder, until })
    return (
      <span className="leasebadge leasebadge--stale" title={lapsed} aria-label={lapsed}>
        <LockIcon open />
        {holder}
      </span>
    )
  }
  // The name carries it, with the glyph for what kind of fact it is and
  // the whole sentence on the label. "Held by" repeated down a column is
  // a word the reader has already read: it costs a third of the chip and
  // says what the colour and the lock already say.
  const held = t('tasks.heldBy', { holder, until })
  return (
    <span className="leasebadge" title={held} aria-label={held}>
      <LockIcon open={false} />
      {holder}
    </span>
  )
}
