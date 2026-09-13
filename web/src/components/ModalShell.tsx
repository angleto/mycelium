import { useEffect, useId, useRef, type ReactNode, type RefObject } from 'react'
import { useTranslation } from 'react-i18next'

// The house modal contract, as a component instead of a convention.
//
// `.modal__backdrop[role=dialog]` + `.modal__panel` + `.modal__head` was
// already the shape every dialog here follows, but the BEHAVIOUR around it
// is written out again at each one, and the copies do not agree: five of
// them close on a bare backdrop `onClick`, so selecting text inside the
// panel and releasing outside discards what you typed, and none of those
// five handles Escape or puts focus inside the dialog at all.
//
// The version that got it right is ConfirmDialog's, and this is it, lifted
// out so the preview dialog does not become a seventh copy of the same
// behaviour. The remaining five are a migration of their own, not
// something to do while passing.
//
// What this owns: the backdrop, the two dismissal paths that are easy to
// get wrong, the labelled panel and its head. What the caller owns: the
// body, the foot, and where focus lands — a confirmation puts it on the
// safe control and a read-only preview on the close button, and nothing
// here can choose between those.
export function ModalShell({
  title,
  headExtra,
  panelClassName,
  initialFocus,
  onClose,
  children,
}: {
  title: ReactNode
  /** Rendered in the head, between the title and the close button. */
  headExtra?: ReactNode
  /** Added to `.modal__panel` (e.g. `modal__panel--narrow`). */
  panelClassName?: string
  /** Takes focus when the dialog opens. Falls back to the close button,
   *  which is always safe: it is the one control that destroys nothing. */
  initialFocus?: RefObject<HTMLElement | null>
  onClose: () => void
  children: ReactNode
}) {
  const { t } = useTranslation()
  const titleId = useId()
  const closeRef = useRef<HTMLButtonElement | null>(null)
  // A backdrop click closes — but only when the press STARTED there.
  // Selecting text inside the panel and releasing outside it would
  // otherwise discard what the user just typed.
  const downOnBackdrop = useRef(false)

  // Escape closes. `stopPropagation` matters: AppShell listens for Escape
  // at the window to close the mobile drawer, and the command palette does
  // the same — without it, dismissing this dialog on a phone would also
  // collapse the sidebar underneath.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      e.stopPropagation()
      onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  // On open, once. A child's ref is assigned before a parent's effect runs,
  // so the caller's choice is already there to be honoured; re-running this
  // would steal focus back from wherever the user has since moved it, which
  // is why the ref, not the prop, is what the effect reads.
  const focusRef = useRef(initialFocus)
  useEffect(() => {
    const wanted = focusRef.current?.current
    if (wanted) wanted.focus()
    else closeRef.current?.focus()
  }, [])

  return (
    <div
      className="modal__backdrop"
      role="dialog"
      aria-modal="true"
      aria-labelledby={titleId}
      onMouseDown={(e) => {
        downOnBackdrop.current = e.target === e.currentTarget
      }}
      onMouseUp={(e) => {
        if (downOnBackdrop.current && e.target === e.currentTarget) onClose()
        downOnBackdrop.current = false
      }}
    >
      <div className={panelClassName ? `modal__panel ${panelClassName}` : 'modal__panel'}>
        <div className="modal__head">
          <strong id={titleId}>{title}</strong>
          {headExtra}
          <span className="modal__sp" />
          <button
            ref={closeRef}
            type="button"
            className="btn--ghost btn--sm"
            aria-label={t('common.close')}
            onClick={onClose}
          >
            ✕
          </button>
        </div>
        {children}
      </div>
    </div>
  )
}
