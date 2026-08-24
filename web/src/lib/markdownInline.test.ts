import { describe, expect, it } from 'vitest'
import { mdLink, mdLinkDestination, mdLinkLabel } from './markdownInline'
import { attachmentMarkdownRef } from './attachmentRef'
import { mentionLink } from './mentions'

// The SPA half of the escaping rule. The Python halves are covered by
// core/tests/test_markdown_inline.py and cli/tests/test_attachment_ref.py;
// all three must agree, so the cases here mirror those on purpose.

// The inverse of `mdLinkLabel`, local to this file on purpose. Nothing in
// the app un-escapes a label any more, and an exported function no caller
// reaches would be exercised by this test rather than covered by it. The
// property is still worth asserting, because the escaper's bug class is
// ORDERING: the backslash has to be escaped first, or the escape added for
// a bracket gets escaped in turn and the bracket comes back out bare.
function unescapeLabel(label: string): string {
  return label.replace(/\\([\s\S])/g, '$1')
}

describe('label escaping', () => {
  it.each([
    ['Report ]final.pdf', String.raw`Report \]final.pdf`],
    ['[draft] notes.md', String.raw`\[draft\] notes.md`],
    [String.raw`back\slash.txt`, String.raw`back\\slash.txt`],
    // Backslash first, or the escape added for the bracket would itself be
    // escaped and the bracket would come back out bare.
    [String.raw`weird\].pdf`, String.raw`weird\\\].pdf`],
    ['plain name.pdf', 'plain name.pdf'],
    ['accenti àèìòù 中文.pdf', 'accenti àèìòù 中文.pdf'],
  ])('%s', (raw, escaped) => {
    expect(mdLinkLabel(raw)).toBe(escaped)
  })

  it('collapses newline runs', () => {
    expect(mdLinkLabel('titolo\n\nsu due righe')).toBe('titolo su due righe')
    expect(mdLinkLabel('titolo\r\nsu due righe')).toBe('titolo su due righe')
  })

  it('round-trips through the unescaper', () => {
    for (const raw of ['Report ]final[.pdf', String.raw`weird\].pdf`, 'plain.pdf']) {
      expect(unescapeLabel(mdLinkLabel(raw))).toBe(raw)
    }
  })
})

describe('destination escaping', () => {
  it('leaves a safe destination alone', () => {
    // `isAttachmentHref` keys on the bare path; growing angle brackets it
    // does not need would break every reference already stored.
    expect(mdLinkDestination('/attachments/abc-123/download')).toBe(
      '/attachments/abc-123/download',
    )
    expect(mdLinkDestination('@task:0f9c')).toBe('@task:0f9c')
  })

  it('wraps one that would break the link', () => {
    expect(mdLinkDestination('/a b/c.png')).toBe('</a b/c.png>')
    expect(mdLinkDestination('/a(b).png')).toBe('</a(b).png>')
    expect(mdLinkDestination('/a<b>.png')).toBe(String.raw`</a\<b\>.png>`)
  })
})

describe('the emitters', () => {
  it('an attachment reference with a bracketed filename stays one link', () => {
    const ref = attachmentMarkdownRef({
      id: 'abc',
      filename: 'Report ]final[.pdf',
      mime_type: 'application/pdf',
    })
    expect(ref).toBe(String.raw`[Report \]final\[.pdf](/attachments/abc/download)`)
  })

  it('an image reference embeds', () => {
    expect(
      attachmentMarkdownRef({ id: 'x', filename: 'shot.png', mime_type: 'image/png' }),
    ).toBe('![shot.png](/attachments/x/download)')
  })

  it('a mention link escapes the title', () => {
    const id = '0f9c6aaa-1111-2222-3333-444444444444'
    expect(mentionLink('task', id, 'Fix ] the parser')).toBe(
      String.raw`[Fix \] the parser](@task:${id})`,
    )
  })
})

describe('mdLink', () => {
  it('builds both shapes', () => {
    expect(mdLink('a', '/x')).toBe('[a](/x)')
    expect(mdLink('a', '/x', { image: true })).toBe('![a](/x)')
  })
})
