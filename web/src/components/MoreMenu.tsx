import { useEffect, useId, useRef, useState, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'

export type MoreMenuItem = {
  label: ReactNode
  onSelect: () => void
  title?: string
  disabled?: boolean
  danger?: boolean
}

// The actions a row carries but does not lead with.
//
// A note row showed seven buttons, every one of them on every row, and
// two of them destroy the note (one of them for good). On a phone that was
// two lines of buttons per note, and the permanent one sat a thumb's width
// from the harmless ones. The row keeps what is done often or reads
// without changing anything; the rest lives here, one tap further, where a
// destructive item is also one deliberate step away.
//
// Menu-button pattern: the trigger says it opens a menu and whether it is
// open, Escape and a press outside close it, opening moves focus to the
// first item and closing returns it to the trigger, and the arrow keys
// move between items.
export function MoreMenu({ items, label }: { items: MoreMenuItem[]; label?: string }) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  const rootRef = useRef<HTMLDivElement | null>(null)
  const triggerRef = useRef<HTMLButtonElement | null>(null)
  const menuRef = useRef<HTMLDivElement | null>(null)
  const menuId = useId()
  const name = label ?? t('common.moreActions')

  useEffect(() => {
    if (!open) return
    const first = menuRef.current?.querySelector<HTMLButtonElement>('button:not(:disabled)')
    first?.focus()
    const onDown = (e: PointerEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      // Kept from the window listeners (the mobile drawer, an enclosing
      // dialog): Escape here closes this menu and nothing under it.
      e.stopPropagation()
      setOpen(false)
      triggerRef.current?.focus()
    }
    document.addEventListener('pointerdown', onDown)
    document.addEventListener('keydown', onKey, true)
    return () => {
      document.removeEventListener('pointerdown', onDown)
      document.removeEventListener('keydown', onKey, true)
    }
  }, [open])

  const onMenuKey = (e: React.KeyboardEvent) => {
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return
    e.preventDefault()
    const btns = [
      ...(menuRef.current?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)') ?? []),
    ]
    const i = btns.indexOf(document.activeElement as HTMLButtonElement)
    const next = e.key === 'ArrowDown' ? (i + 1) % btns.length : (i - 1 + btns.length) % btns.length
    btns[next]?.focus()
  }

  return (
    <div className="moremenu" ref={rootRef}>
      <button
        ref={triggerRef}
        type="button"
        className="btn--ghost btn--sm moremenu__trigger"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        aria-label={name}
        title={name}
        onClick={() => setOpen((v) => !v)}
      >
        <span aria-hidden="true">⋯</span>
      </button>
      {open && (
        <div
          id={menuId}
          ref={menuRef}
          role="menu"
          aria-label={name}
          className="moremenu__list"
          onKeyDown={onMenuKey}
        >
          {items.map((it, i) => (
            <button
              key={i}
              type="button"
              role="menuitem"
              className={'moremenu__item' + (it.danger ? ' moremenu__item--danger' : '')}
              title={it.title}
              disabled={it.disabled}
              onClick={() => {
                setOpen(false)
                triggerRef.current?.focus()
                it.onSelect()
              }}
            >
              {it.label}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
