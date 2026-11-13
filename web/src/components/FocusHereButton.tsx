import { useTranslation } from 'react-i18next'
import { useFocus } from '../lib/focus'
import type { components } from '../shared'

type TagBrief = components['schemas']['TagBrief']

// "Focus on this": point the sidebar focus at the client and project the
// open task or note belongs to, so the rest of the app narrows to the
// work it is part of.
//
// The pair is read off the item's structural tags rather than asked for:
// a task carries exactly one client and one project, a note one client and
// at most one project, so there is nothing to choose and the button is one
// click. A note without a project focuses its whole client, which is what
// the sidebar calls that same state.
//
// It is a TOGGLE, and says so (aria-pressed): when the current focus is
// already exactly this item's pair it shows as on, and pressing it again
// leaves focus. Without that state the button would look the same whether
// or not it had done anything, and a second click would be a silent no-op.
export function FocusHereButton({ tags }: { tags: TagBrief[] | undefined }) {
  const { t } = useTranslation()
  const { clientId, projectId, setFocus, clear } = useFocus()
  const client = tags?.find((g) => g.kind === 'client')
  // Nothing to focus on: not rendered rather than disabled, because there
  // is no state of this item in which it would become available.
  if (!client) return null
  const project = tags?.find((g) => g.kind === 'project')
  const on = clientId === client.id && projectId === (project?.id ?? '')
  const title = on
    ? t('focus.hereLeaveTitle')
    : project
      ? t('focus.hereTitle', { client: client.name, project: project.name })
      : t('focus.hereTitleClient', { client: client.name })
  return (
    <button
      type="button"
      className={`btn--ghost btn--sm focushere${on ? ' is-on' : ''}`}
      aria-pressed={on}
      title={title}
      onClick={() =>
        on ? clear() : setFocus(client.id, client.name, project?.id ?? '')
      }
    >
      <span aria-hidden="true">◎</span> {on ? t('focus.hereOn') : t('focus.here')}
    </button>
  )
}
