import { useTranslation } from 'react-i18next'
import { CopyIdButton } from './CopyIdButton'
import { PeekButton } from './PeekButton'
import { TaskTimer } from './TaskTimer'

// The controls that save a round trip through a task's detail page: take
// its id, read it over the current view, start (or start in parallel) its
// timer. Every surface that lists tasks shows the same three in the same
// order, so they live here once rather than being re-assembled per view,
// where the copies drift (a label changed in one and not the other).
//
// It renders the controls bare, as a fragment: the wrapper belongs to the
// caller, because it is the caller that knows the geometry (a list row's
// trailing cluster, a kanban card's wrapping corner, a second line under a
// recent row) and whether clicks must be kept from reaching a drag handler.
export function TaskQuickActions({ id, title }: { id: string; title: string }) {
  const { t } = useTranslation()
  return (
    <>
      <CopyIdButton
        id={id}
        variant="icon"
        label={t('tasks.copyId')}
        copiedLabel={t('tasks.idCopied')}
      />
      <PeekButton target={{ kind: 'task', id, title }} />
      <TaskTimer taskId={id} />
    </>
  )
}
