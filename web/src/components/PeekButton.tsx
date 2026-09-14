import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { ModalShell } from './ModalShell'
import { NoteDetailRoute } from '../routes/NoteDetailRoute'
import { TaskDetailRoute } from '../routes/TaskDetailRoute'

/** What a row knows about itself before anything is fetched. The title
 *  comes from the list payload so the dialog can name itself immediately,
 *  rather than opening as an empty box that fills in. */
export type PeekTarget = { kind: 'task' | 'note'; id: string; title: string }

// Open a task or a note over the list, without leaving it.
//
// The cost this removes is not the rendering, it is the RETURN: opening a
// task from a search meant navigating away and then coming back to a list
// that had to be rebuilt, with the scroll position and the query gone. So
// this never touches the router — it is a dialog over the list, and the
// list is still underneath it when it closes.
//
// It mounts THE DETAIL SCREEN, not a rendering of it. The first version
// drew its own: a markdown view instead of the editor, a disabled checkbox
// list instead of the checklist panel, no annotations, no mention chips, no
// properties. Everything about it was a second implementation of a screen
// that already existed, and the way that shows is that it looked like the
// task without being it. `TaskDetailRoute` / `NoteDetailRoute` take an id
// and an `embedded` flag for exactly this: one component, two mounts.
export function PeekButton({ target }: { target: PeekTarget }) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  return (
    <>
      <button
        type="button"
        className="btn--ghost btn--sm peek"
        title={t('common.peek')}
        aria-label={t('common.peek')}
        aria-expanded={open}
        onClick={() => setOpen(true)}
      >
        <span aria-hidden="true">👁</span>
      </button>
      {open && (
        <ModalShell
          title={target.title}
          panelClassName="modal__panel--detail peek__panel"
          onClose={() => setOpen(false)}
        >
          <div className="modal__body peek__body">
            {target.kind === 'task' ? (
              <TaskDetailRoute id={target.id} embedded onLeave={() => setOpen(false)} />
            ) : (
              <NoteDetailRoute id={target.id} embedded onLeave={() => setOpen(false)} />
            )}
          </div>
        </ModalShell>
      )}
    </>
  )
}
