import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { copyToClipboard } from '../lib/clipboard'

// The "copy this row's id" control, in one place.
//
// It existed four times (both detail headers, the garden modal, the parts
// editor), each with its own useState + setTimeout and a bare
// `navigator.clipboard.writeText` whose failure was swallowed. They all
// drew the same chip, so the fifth copy — on a card, which is what this
// component was added for: an id you can take without opening the thing —
// is the one that pays for collapsing them.
//
// Two shapes, because the two places differ in what they can spend.
// `chip` names itself (`ID 1a2b3c4d…`) and belongs in a detail header,
// where there is room and the id is worth showing. `icon` is a single
// glyph for a row in a list, where a labelled chip would compete with the
// title. The accessible name carries the meaning in both, and the title
// attribute carries the full id.

const FLASH_MS = 1500
/** A failure stays up longer: it is the state the user has to act on. */
const FAIL_FLASH_MS = 5000

export function CopyIdButton({
  id,
  label,
  copiedLabel,
  variant = 'chip',
}: {
  id: string
  /** Accessible name ("Copy task ID"). The KIND is the caller's to name:
   *  the same component copies a task, a note and a note part. */
  label: string
  copiedLabel: string
  variant?: 'chip' | 'icon'
}) {
  const { t } = useTranslation()
  const [state, setState] = useState<'idle' | 'done' | 'failed'>('idle')
  const timer = useRef<number | undefined>(undefined)

  // A row can be unmounted by a filter or a reload while the flash is still
  // pending; without this the timeout fires into a dead component.
  useEffect(() => () => window.clearTimeout(timer.current), [])

  async function onCopy(): Promise<void> {
    // Never a bare writeText: outside a secure context there is no
    // clipboard object at all, and the four call sites this replaces all
    // said "copied" anyway. copyToClipboard says what actually happened.
    const ok = await copyToClipboard(id, t('common.copyManual'))
    setState(ok ? 'done' : 'failed')
    window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => setState('idle'), ok ? FLASH_MS : FAIL_FLASH_MS)
  }

  const flash =
    state === 'done' ? copiedLabel : state === 'failed' ? t('common.copyFailed') : null
  const glyph = state === 'done' ? '✓' : state === 'failed' ? '✕' : '⧉'
  return (
    <button
      type="button"
      className={variant === 'chip' ? 'chip chip--copy' : 'btn--ghost btn--sm copyid'}
      // The full id, so it is readable even where the clipboard is
      // unavailable and the visible label is one glyph.
      title={flash ?? id}
      aria-label={label}
      onClick={() => void onCopy()}
    >
      {variant === 'chip' ? (
        (flash ?? `ID ${id.slice(0, 8)}…`)
      ) : (
        <span aria-hidden="true">{glyph}</span>
      )}
      {/* The outcome reaches assistive technology in the icon shape too,
          where the glyph above is decorative and the label never changes. */}
      <span className="sr-only" role="status" aria-live="polite">
        {flash ?? ''}
      </span>
    </button>
  )
}
