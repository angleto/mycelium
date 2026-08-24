import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { act, type ReactNode } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MarkdownView } from './Markdown'
import { isAttachmentHref } from '../lib/attachmentRef'

// The invariant that lets the global click interceptor stay out of the
// attachment business.
//
// `/attachments/<id>/download` is bearer-authenticated: navigating to it in
// the clear answers 401 and the reader gets nothing, silently. So every
// anchor pointing at an attachment has to be handled in React by whatever
// rendered it, and `md-att` is both the marker and the promise that a click
// handler is attached.
//
// AppShell used to carry a capture-phase fallback that caught any anchor
// this renderer left unhandled. It was there because the document-model
// editor put plain <a> marks in the DOM with no handler on them; that editor
// is gone, its replacement renders no anchors at all, and the fallback was
// deleted once MarkdownView became the only thing producing a link a reader
// can click. This test is what makes that deletion safe.
//
// A failure here is not fixed by relaxing the assertion: either the renderer
// regressed, or the fallback has to come back.

const PARENT = {
  kind: 'note',
  id: '9c1f0b62-0000-4000-8000-000000000001',
} as const
const CANONICAL = '/attachments/11111111-1111-1111-1111-111111111111/download'
const BARE = 'Fig02_donne.png'
const EXTERNAL = 'https://example.com/x.pdf'

// Link syntax only, deliberately. An `![...]` reference goes through
// AuthMedia, which fetches the bytes before it can decide what to render,
// and this suite has no network.
const BODY = [
  `[doc.pdf](${CANONICAL})`,
  `[doc.pdf, again](${CANONICAL}?v=2)`,
  `[the figure](${BARE})`,
  `[esterno](${EXTERNAL})`,
  '[ancora](#sezione)',
].join('\n\n')

let host: HTMLDivElement
let root: Root

beforeEach(() => {
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
})

afterEach(() => {
  act(() => {
    root.unmount()
  })
  host.remove()
})

function render(ui: ReactNode): HTMLAnchorElement[] {
  act(() => {
    root.render(ui)
  })
  return Array.from(host.querySelectorAll('a'))
}

/** The classes on the anchor for `href`, or null when it was not rendered.
 *  Null rather than a throw, so a missing anchor fails the assertion that
 *  wanted it instead of aborting the test. */
function classesOf(anchors: HTMLAnchorElement[], href: string): string[] | null {
  const a = anchors.find((x) => x.getAttribute('href') === href)
  return a ? Array.from(a.classList) : null
}

describe('MarkdownView leaves no attachment link unhandled', () => {
  it.each([
    ['with a parent', PARENT],
    ['without one', undefined],
  ])('marks every canonical attachment href, %s', (_label, parent) => {
    const anchors = render(<MarkdownView text={BODY} parent={parent} />)
    const attachments = anchors.filter((a) =>
      isAttachmentHref(a.getAttribute('href')),
    )
    // Asserted as the SET of hrefs, not as a count: this also fails if one
    // of them stops being rendered at all, which would otherwise let the
    // loop below pass over an empty list. Both the plain route and the one
    // carrying a query string are here because the href matcher accepts
    // both, so the renderer has to handle both.
    expect(attachments.map((a) => a.getAttribute('href')).sort()).toEqual(
      [CANONICAL, `${CANONICAL}?v=2`].sort(),
    )
    for (const a of attachments) {
      expect(Array.from(a.classList)).toContain('md-att')
    }
  })

  it('marks a bare filename once a parent can resolve it', () => {
    const anchors = render(<MarkdownView text={BODY} parent={PARENT} />)
    expect(classesOf(anchors, BARE)).toContain('md-att')
  })

  it('leaves an ordinary link ordinary, so the assertions are not vacuous', () => {
    const anchors = render(<MarkdownView text={BODY} parent={PARENT} />)
    expect(classesOf(anchors, EXTERNAL)).not.toContain('md-att')
    expect(classesOf(anchors, '#sezione')).not.toContain('md-att')
  })

  // The declared boundary. With no parent there is nothing to resolve a bare
  // filename against, so it stays an ordinary link. That is not a hole the
  // deleted fallback used to plug: its bare-filename branch keyed on an
  // attribute only the editor stamped, and the editor handed MarkdownView
  // the very same parent, so the two conditions could never both hold.
  it('leaves a bare filename alone when no parent can resolve it', () => {
    const anchors = render(<MarkdownView text={BODY} />)
    expect(classesOf(anchors, BARE)).not.toContain('md-att')
  })
})
