import { useId, useRef, useState, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { ModalShell } from './ModalShell'

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
// The shell is ModalShell, which was lifted out of this file: the three
// dismissal paths the e2e suite already checks elsewhere — the header
// close button, Escape and a backdrop click — live there now, and what
// stays here is only what a CONFIRMATION adds to a dialog.
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
  const formId = useId()
  const [typed, setTyped] = useState('')
  const inputRef = useRef<HTMLInputElement | null>(null)
  const cancelRef = useRef<HTMLButtonElement | null>(null)

  const armed = !confirmWord || typed.trim() === confirmWord.trim()

  return (
    <ModalShell
      title={title}
      panelClassName="modal__panel--narrow modal__panel--auto"
      // Focus lands on the safe control (the proof-of-intent field when
      // there is one, the Cancel button otherwise) — never on the
      // destructive button, which a stray Enter would then fire.
      initialFocus={confirmWord ? inputRef : cancelRef}
      onClose={onClose}
    >
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
    </ModalShell>
  )
}
