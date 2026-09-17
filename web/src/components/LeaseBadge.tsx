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
// Two renderings, not one, because the deadline is not decoration: past
// it the server hands the task to whoever asks, and calling that "held"
// sends the reader off to wait for something that already happened. The
// predicate lives in shared/leases, next to the one the banner uses.
// ``undefined`` from a board's map and ``null`` from the one-task
// question mean the same thing, so both are accepted rather than made
// the caller's problem to normalise.
export function LeaseBadge({ possession }: { possession: Possession | null | undefined }) {
  const { t } = useTranslation()
  if (!possession) return null
  const holder = possession.lease.holder_label
  const until = fmtDateTime(possession.lease.expires_at)
  if (possession.kind === 'stale') {
    return (
      <span
        className="leasebadge leasebadge--stale"
        title={t('tasks.heldLapsedTitle', { holder, until })}
      >
        {t('tasks.heldLapsed')}
      </span>
    )
  }
  return (
    <span className="leasebadge" title={t('tasks.heldBy', { holder, until })}>
      {t('tasks.heldByShort', { holder })}
    </span>
  )
}
