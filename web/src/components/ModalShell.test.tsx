import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, useRef } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { ModalShell } from './ModalShell'
import '../i18n'

// The behaviour that was hand-written at every dialog and is now written
// once. It is asserted here rather than only end to end because the five
// copies that stayed behind disagree with this one on every line below,
// and a shell nobody tests would drift back into being a sixth.

let host: HTMLDivElement
let root: Root

function mount(node: React.ReactElement): void {
  act(() => root.render(node))
}

function backdrop(): HTMLElement {
  const el = host.querySelector('.modal__backdrop')
  if (!(el instanceof HTMLElement)) throw new Error('no backdrop')
  return el
}

function panel(): HTMLElement {
  const el = host.querySelector('.modal__panel')
  if (!(el instanceof HTMLElement)) throw new Error('no panel')
  return el
}

/** A press that starts on `down` and is released on `up`, which is the
 *  distinction a bare `onClick` cannot make. */
function press(down: HTMLElement, up: HTMLElement): void {
  act(() => {
    down.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }))
    up.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }))
  })
}

beforeEach(() => {
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
})

afterEach(() => {
  act(() => root.unmount())
  host.remove()
})

describe('ModalShell', () => {
  it('is a dialog named by its own title', () => {
    mount(
      <ModalShell title="Una nota" onClose={() => {}}>
        <div className="modal__body">corpo</div>
      </ModalShell>,
    )
    const el = backdrop()
    expect(el.getAttribute('role')).toBe('dialog')
    expect(el.getAttribute('aria-modal')).toBe('true')
    const labelId = el.getAttribute('aria-labelledby')
    expect(labelId).toBeTruthy()
    expect(host.querySelector(`#${CSS.escape(labelId as string)}`)?.textContent).toBe(
      'Una nota',
    )
  })

  it('closes on Escape, without letting the key reach the app underneath', () => {
    // AppShell listens for Escape at the window to close the mobile
    // drawer, and the command palette does the same: dismissing a dialog
    // on a phone must not also collapse the sidebar behind it.
    const onClose = vi.fn()
    const atWindow = vi.fn()
    window.addEventListener('keydown', atWindow)
    mount(
      <ModalShell title="x" onClose={onClose}>
        <div className="modal__body" />
      </ModalShell>,
    )
    act(() => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    })
    window.removeEventListener('keydown', atWindow)
    expect(onClose).toHaveBeenCalledTimes(1)
    expect(atWindow).not.toHaveBeenCalled()
  })

  it('closes on a backdrop press, but only one that STARTED on the backdrop', () => {
    // Selecting text inside the panel and releasing outside it is the
    // gesture a bare `onClick` reads as "close", discarding whatever the
    // user had typed.
    const onClose = vi.fn()
    mount(
      <ModalShell title="x" onClose={onClose}>
        <div className="modal__body">testo</div>
      </ModalShell>,
    )
    press(panel(), backdrop())
    expect(onClose).not.toHaveBeenCalled()
    press(backdrop(), backdrop())
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('puts focus on the close button when the caller names nothing safer', () => {
    mount(
      <ModalShell title="x" onClose={() => {}}>
        <div className="modal__body">
          <button type="button">pericoloso</button>
        </div>
      </ModalShell>,
    )
    // Never the first focusable thing in the body: that is where a
    // destructive action sits, and a stray Enter would fire it.
    expect(document.activeElement?.getAttribute('aria-label')).toBe('Close')
  })

  it('honours the control the caller asked to focus', () => {
    function WithField() {
      const ref = useRef<HTMLInputElement | null>(null)
      return (
        <ModalShell title="x" initialFocus={ref} onClose={() => {}}>
          <div className="modal__body">
            <input ref={ref} aria-label="conferma" />
          </div>
        </ModalShell>
      )
    }
    mount(<WithField />)
    expect(document.activeElement?.getAttribute('aria-label')).toBe('conferma')
  })
})
