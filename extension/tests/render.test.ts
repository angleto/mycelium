// Rendering content that came from the workspace.
//
// A search snippet is the sharpest edge in this package. The server marks
// the match with literal <b> tags and does NOT escape the text around
// them, so a note containing an image tag arrives verbatim. Rendering the
// extract as markup would execute whatever somebody wrote in a note --
// including a note somebody else wrote and shared.

import { describe, expect, it } from 'vitest'
import { headline } from '../src/ui/dom'
import { loadingText } from '../src/ui/outcome'
import { installFakeChrome } from './fake-chrome'

installFakeChrome()

function render(snippet: string): HTMLElement {
  const host = document.createElement('div')
  host.appendChild(headline(snippet))
  return host
}

describe('a server snippet', () => {
  it('marks the match and leaves the rest as text', () => {
    const host = render('un <b>preventivo</b> qui')
    expect(host.textContent).toBe('un preventivo qui')
    expect(host.querySelectorAll('mark')).toHaveLength(1)
    expect(host.querySelector('mark')?.textContent).toBe('preventivo')
  })

  it('renders an injected tag as the characters it is', () => {
    const host = render('before <img src=x onerror=alert(1)> after')
    expect(host.querySelectorAll('img')).toHaveLength(0)
    expect(host.textContent).toContain('<img src=x onerror=alert(1)>')
  })

  it('never produces an element other than the mark', () => {
    const host = render('<b>a</b><script>alert(1)</script><b>b</b>')
    const tags = [...host.querySelectorAll('*')].map((n) => n.tagName.toLowerCase())
    expect(new Set(tags)).toEqual(new Set(['mark']))
    expect(host.textContent).toContain('<script>alert(1)</script>')
  })

  it('degrades to plain text on unbalanced delimiters rather than swallowing the rest', () => {
    const host = render('an <b>unclosed match and the rest of the note')
    expect(host.textContent).toBe('an <b>unclosed match and the rest of the note')
  })

  it('handles an empty extract', () => {
    expect(render('').textContent).toBe('')
  })
})

describe('loading has three forms, and the boundary is time', () => {
  it('shows nothing at all below 300ms, because a flash reads as a glitch', () => {
    expect(loadingText(1000, 1200)).toBeNull()
  })

  it('says it is searching once the wait is noticeable', () => {
    expect(loadingText(1000, 1500)).toBe('Searching…')
  })

  it('names the REASON past two seconds', () => {
    // The server's semantic leg is time-boxed at two seconds and the
    // first search after a deployment is genuinely slow. Saying "the
    // index is waking up" turns a bug report into a wait.
    expect(loadingText(1000, 4000)).toContain('waking up')
  })
})
