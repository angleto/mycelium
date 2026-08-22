import { createRoot } from 'react-dom/client'
import { createElement } from 'react'
import { MarkdownView } from '../../components/Markdown'
import type { ImageUploadParent } from '../imageUpload'

// HTML for the PDF export, rendered by the READ-SIDE renderer.
//
// It used to be `editor.getHTML()` off the tiptap tree, which had two
// problems even before that tree went away. The PDF matched neither /garden
// nor the note reader, because it came from a second renderer; and it
// inherited whatever the editor's node views happened to emit.
//
// So the export now renders the same component the reader does. The PDF
// becomes what the note looks like, which is what anybody exporting one
// expects, and there is one renderer instead of two.
//
// The awkward part, and the reason this is not four lines: the render is
// ASYNCHRONOUS. `useAttachmentImage` resolves its object URL after an
// authenticated fetch, and `Mermaid` sits behind a dynamic import and
// debounces. Reading `innerHTML` straight after `root.render(...)` exports a
// document with no images and no diagrams -- silently, and only for the
// people who use them.

/** No DOM mutation for this long means the render has settled. */
const QUIET_MS = 150
/** Hard stop. A diagram that never resolves must not hang the export; the
 *  PDF then simply lacks it, which is better than a spinner forever. */
const MAX_WAIT_MS = 5_000

function waitForQuiescence(el: HTMLElement): Promise<void> {
  return new Promise((resolve) => {
    let quiet: number | null = null
    const done = () => {
      if (quiet !== null) window.clearTimeout(quiet)
      window.clearTimeout(cap)
      obs.disconnect()
      resolve()
    }
    const bump = () => {
      if (quiet !== null) window.clearTimeout(quiet)
      quiet = window.setTimeout(done, QUIET_MS)
    }
    const obs = new MutationObserver(bump)
    obs.observe(el, { childList: true, subtree: true, attributes: true, characterData: true })
    const cap = window.setTimeout(done, MAX_WAIT_MS)
    bump()
  })
}

/**
 * Render `markdown` the way the reader does and return the resulting HTML.
 *
 * Off-screen rather than hidden: `display: none` would leave images unloaded
 * and give a mermaid diagram no box to measure itself in, so the quiescence
 * wait would end on a document that never actually rendered.
 */
export async function renderMarkdownToHtml(
  markdown: string,
  parent?: ImageUploadParent,
): Promise<string> {
  const host = document.createElement('div')
  host.style.position = 'fixed'
  host.style.left = '-10000px'
  host.style.top = '0'
  host.style.width = '800px'
  host.setAttribute('aria-hidden', 'true')
  document.body.append(host)
  const root = createRoot(host)
  try {
    root.render(createElement(MarkdownView, { text: markdown, parent }))
    await waitForQuiescence(host)
    return host.innerHTML
  } finally {
    // React refuses a synchronous unmount from inside a commit, and the
    // caller may well be one; the microtask is the documented escape. The
    // host is removed with it, not before, or React would tear down against
    // a detached tree.
    queueMicrotask(() => {
      root.unmount()
      host.remove()
    })
  }
}
