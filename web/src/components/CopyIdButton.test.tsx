import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { CopyIdButton } from './CopyIdButton'
import '../i18n'

// What a copy button owes the user: the id on the clipboard, and the
// truth about whether it got there.
//
// The four hand-rolled copies this component replaces all did
// `await navigator.clipboard.writeText(id)` in a try/catch and flashed
// "ID copied" on success only — but two of the three ways that call fails
// are invisible to the user (no clipboard object outside a secure context,
// a denied permission), and the catch blocks set the flag back to false
// without saying anything. So the assertion that matters here is the
// failure one: a button that copied nothing must not claim it did.

let host: HTMLDivElement
let root: Root

function render(node: React.ReactElement): HTMLElement {
  act(() => root.render(node))
  const el = host.querySelector('button')
  if (!el) throw new Error('no button rendered')
  return el
}

beforeEach(() => {
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
})

afterEach(() => {
  act(() => root.unmount())
  host.remove()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

const ID = '7b2ea76f-a08e-460c-854f-1ec265aceaec'

function stubClipboard(writeText: () => Promise<void>): void {
  vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } })
}

describe('CopyIdButton', () => {
  it('puts the whole id on the clipboard, not the shortened label', async () => {
    const written: string[] = []
    stubClipboard(async (...args: unknown[]) => void written.push(String(args[0])))
    const btn = render(
      <CopyIdButton id={ID} label="Copy task ID" copiedLabel="ID copied" />,
    )
    // The visible label is truncated; what is copied is the handle.
    expect(btn.textContent).toContain('7b2ea76f…')
    await act(async () => btn.click())
    expect(written).toEqual([ID])
    expect(btn.textContent).toContain('ID copied')
  })

  it('says so when the clipboard refuses, and does not claim a copy', async () => {
    // The shape of a denied permission or an insecure context. The
    // fallbacks in lib/clipboard.ts also fail under jsdom (execCommand is
    // unimplemented, window.prompt throws), which is exactly the "nothing
    // reached the clipboard" case the UI has to be honest about.
    stubClipboard(() => Promise.reject(new Error('denied')))
    const btn = render(
      <CopyIdButton id={ID} label="Copy task ID" copiedLabel="ID copied" />,
    )
    await act(async () => btn.click())
    expect(btn.textContent).toContain('Copy failed')
    expect(btn.textContent).not.toContain('ID copied')
  })

  it('carries the full id and a stable accessible name in the icon shape', async () => {
    stubClipboard(async () => {})
    const btn = render(
      <CopyIdButton
        id={ID}
        variant="icon"
        label="Copy task ID"
        copiedLabel="ID copied"
      />,
    )
    // One glyph, so the name and the id have to live in the attributes:
    // the label never changes, and the title carries the id until there
    // is an outcome to report instead.
    expect(btn.getAttribute('aria-label')).toBe('Copy task ID')
    expect(btn.getAttribute('title')).toBe(ID)
    await act(async () => btn.click())
    expect(btn.getAttribute('aria-label')).toBe('Copy task ID')
    expect(btn.getAttribute('title')).toBe('ID copied')
    // And the outcome is announced, since the glyph is aria-hidden.
    expect(btn.querySelector('[role="status"]')?.textContent).toBe('ID copied')
  })
})
