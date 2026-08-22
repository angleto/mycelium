import { beforeEach, describe, expect, it } from 'vitest'
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import {
  __resetUnsavedGuard,
  hasUnsavedEdits,
  useUnsavedGuard,
} from './unsavedGuard'

// The register is what stands between an automatic reload onto a new
// build (useBuildWatch) and someone's half-written note, so its failure
// modes cut both ways: a LEAKED entry blocks every future reload, and the
// user is back to hard-reloading by hand; a DROPPED entry lets the reload
// through while text is unsaved. Both are asserted below on a real mount,
// because both live in the effect's lifecycle, not in the Set.

function Editor({ dirty }: { dirty: boolean }) {
  useUnsavedGuard(dirty)
  return null
}

let host: HTMLDivElement
let root: Root

beforeEach(() => {
  __resetUnsavedGuard()
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
})

function render(ui: React.ReactNode) {
  act(() => {
    root.render(ui)
  })
}

function unmount() {
  act(() => {
    root.unmount()
  })
  host.remove()
}

describe('unsavedGuard', () => {
  it('reports nothing unsaved when no editor is mounted', () => {
    expect(hasUnsavedEdits()).toBe(false)
  })

  it('registers a dirty editor and clears it when the edits are saved', () => {
    render(<Editor dirty={true} />)
    expect(hasUnsavedEdits()).toBe(true)
    render(<Editor dirty={false} />)
    expect(hasUnsavedEdits()).toBe(false)
    unmount()
  })

  it('does not register a clean editor', () => {
    render(<Editor dirty={false} />)
    expect(hasUnsavedEdits()).toBe(false)
    unmount()
  })

  it('releases the block when a dirty editor unmounts', () => {
    // A route change away from a dirty task must not strand the entry:
    // it would silently disable automatic reloads for the rest of the
    // session, and nothing on screen would explain why.
    render(<Editor dirty={true} />)
    expect(hasUnsavedEdits()).toBe(true)
    unmount()
    expect(hasUnsavedEdits()).toBe(false)
  })

  it('stays blocked while any one of several editors is dirty', () => {
    render(
      <>
        <Editor dirty={true} />
        <Editor dirty={false} />
      </>,
    )
    expect(hasUnsavedEdits()).toBe(true)
    // The clean one going dirty and the dirty one going clean must not
    // cancel each other out through a shared key.
    render(
      <>
        <Editor dirty={false} />
        <Editor dirty={true} />
      </>,
    )
    expect(hasUnsavedEdits()).toBe(true)
    render(
      <>
        <Editor dirty={false} />
        <Editor dirty={false} />
      </>,
    )
    expect(hasUnsavedEdits()).toBe(false)
    unmount()
  })
})
