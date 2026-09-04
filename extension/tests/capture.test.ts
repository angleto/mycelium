// The captured link.
//
// One line of this package is genuinely security-relevant: the URL of the
// page you were reading becomes a markdown link inside a task description
// or a note, and that text is later rendered by the app, the command-line
// client and the editor plugin. Outside input reaching a URL parser, where
// the scheme is syntax.
//
// So the scheme is chosen from a SET enumerated in the source rather than
// filtered for the one bad value somebody remembered. There is no list of
// dangerous schemes here, and that is the point: a scheme nobody thought
// of is refused by default.

import { describe, expect, it } from 'vitest'
import { sourceLine } from '../src/bg/capture'
import { installFakeChrome } from './fake-chrome'

installFakeChrome()

describe('the source line', () => {
  it('links an ordinary page', () => {
    expect(sourceLine('https://example.test/a', 'A page')).toBe('[A page](https://example.test/a)')
  })

  it('accepts only http and https, whatever else arrives', () => {
    for (const url of [
      'javascript:alert(1)',
      'data:text/html,<script>alert(1)</script>',
      'chrome://extensions',
      'file:///etc/passwd',
      'vbscript:msgbox(1)',
      'blob:https://example.test/abc',
    ]) {
      expect(sourceLine(url, 'x'), url).toBe('')
    }
  })

  it('escapes a title that would close the link early', () => {
    const line = sourceLine('https://example.test/', 'A [bracketed] title')
    expect(line).toBe('[A \\[bracketed\\] title](https://example.test/)')
  })

  it('flattens a multi-line title, which would break the link across lines', () => {
    expect(sourceLine('https://example.test/', 'two\nlines')).toBe(
      '[two lines](https://example.test/)',
    )
  })

  it('encodes parentheses in the target, which would close the link', () => {
    const line = sourceLine('https://example.test/a(b)c', 'x')
    expect(line).toContain('%28')
    expect(line).toContain('%29')
    expect(line.endsWith(')')).toBe(true)
    // Exactly one closing parenthesis: the link's own.
    expect(line.split(')').length - 1).toBe(1)
  })

  it('falls back to the host when there is no title to show', () => {
    expect(sourceLine('https://example.test/a', null)).toBe('[example.test](https://example.test/a)')
  })

  it('says nothing at all when there is no page', () => {
    expect(sourceLine(null, 'x')).toBe('')
    expect(sourceLine('not a url', 'x')).toBe('')
  })
})
