import { useEffect, useId, useRef, useState, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'

// An in-DOM confirmation dialog for actions that cannot be undone.
//
// It replaces `window.confirm` where the stakes justify it. Three
// reasons, in order of weight:
//   1. a native confirm cannot say WHAT is about to be destroyed — it
//      gets one line of text and no structure;
//   2. it cannot ask the user to prove they mean it (`confirmWord`);
//   3. Playwright auto-DISMISSES native dialogs unless a spec installs
//      a handler, so every `window.confirm` path in this SPA is
//      currently untestable end to end. This one is assertable.
//
// The markup follows the house modal contract (`.modal__backdrop[role=
// dialog]` + `.modal__panel`), so the three dismissal paths the e2e
// suite already checks elsewhere — the header close button, Escape and
// a backdrop click — all work here too.
export function ConfirmDialog({
  title,
  intro,
  children,
  confirmLabel,
  confirmWord,
  confirmWordHint,
  danger = false,
  busy = false,
  error,
  onConfirm,
  onClose,
}: {
  title: string
  /** The one-sentence statement of what is about to happen. */
  intro: string
  /** Optional detail: what exactly is destroyed, a warning list, etc. */
  children?: ReactNode
  confirmLabel: string
  /** When set, the confirm button stays disabled until the user types
   * this exact text. Reserved for the genuinely irreversible: it makes
   * the user name the thing they are destroying. */
  confirmWord?: string
  confirmWordHint?: string
  danger?: boolean
  busy?: boolean
  error?: string | null
  onConfirm: () => void
  onClose: () => void
}) {
  const { t } = useTranslation()
  const titleId = useId()
  const formId = useId()
  const [typed, setTyped] = useState('')
  const inputRef = useRef<HTMLInputElement | null>(null)
  const cancelRef = useRef<HTMLButtonElement | null>(null)
  // A backdrop click closes — but only when the press STARTED there.
  // Selecting the workspace name inside the panel and releasing outside
  // it would otherwise discard what the user just typed.
  const downOnBackdrop = useRef(false)

  // Escape closes. `stopPropagation` matters: AppShell listens for
  // Escape at the window to close the mobile drawer, and the command
  // palette does the same — without it, dismissing this dialog on a
  // phone would also collapse the sidebar underneath.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      e.stopPropagation()
      onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  // Focus lands on the safe control (the proof-of-intent field when
  // there is one, the Cancel button otherwise) — never on the
  // destructive button, which a stray Enter would then fire.
  useEffect(() => {
    if (inputRef.current) inputRef.current.focus()
    else cancelRef.current?.focus()
  }, [])

  const armed = !confirmWord || typed.trim() === confirmWord.trim()

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
      <div className="modal__panel modal__panel--narrow modal__panel--auto">
        <div className="modal__head">
          <strong id={titleId}>{title}</strong>
          <span className="modal__sp" />
          <button
            type="button"
            className="btn--ghost btn--sm"
            aria-label={t('wsmgr.cancel')}
            onClick={onClose}
          >
            ✕
          </button>
        </div>
        <form
          id={formId}
          className="modal__body"
          onSubmit={(e) => {
            e.preventDefault()
            if (armed && !busy) onConfirm()
          }}
        >
          <p className={danger ? 'confirm__intro confirm__intro--danger' : 'confirm__intro'}>
            {intro}
          </p>
          {children}
          {confirmWord && (
            <label>
              {confirmWordHint ?? t('wsmgr.typeToConfirm', { name: confirmWord })}
              <input
                ref={inputRef}
                value={typed}
                autoComplete="off"
                spellCheck={false}
                onChange={(e) => setTyped(e.target.value)}
              />
            </label>
          )}
          {error && <p className="err">{error}</p>}
        </form>
        {/* The foot is a SIBLING of the body, not a child: `.modal__body`
            is the panel's only scroller, so a footer inside it scrolls
            away with the content and its top border lands in the body
            padding. `form={formId}` keeps the submit button wired to the
            form it sits outside of, so Enter in the field still
            confirms. */}
        <div className="modal__foot">
          <button
            ref={cancelRef}
            type="button"
            className="btn--ghost"
            onClick={onClose}
          >
            {t('wsmgr.cancel')}
          </button>
          <span className="modal__sp" />
          <button
            type="submit"
            form={formId}
            className={danger ? 'btn--danger' : undefined}
            disabled={!armed || busy}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  )
}
