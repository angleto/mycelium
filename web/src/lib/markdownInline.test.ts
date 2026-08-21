import { describe, expect, it } from 'vitest'
import { mdLink, mdLinkDestination, mdLinkLabel, mdUnescapeLabel } from './markdownInline'
import { attachmentMarkdownRef, parseAttachmentMarkdownRef } from './attachmentRef'
import { mentionLink } from './mentions'

// The SPA half of the escaping rule. The Python halves are covered by
// core/tests/test_markdown_inline.py and cli/tests/test_attachment_ref.py;
// all three must agree, so the cases here mirror those on purpose.

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
      expect(mdUnescapeLabel(mdLinkLabel(raw))).toBe(raw)
    }
  })
})

describe('destination escaping', () => {
  it('leaves a safe destination alone', () => {
    // The matcher keys on the bare path; growing angle brackets it does not
    // need would break every existing reference.
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

describe('the matcher and the emitter stay symmetric', () => {
  // The matcher's label class had to learn backslash escapes at the same
  // time as the emitter started producing them. If it had not, pasting back
  // the exact reference the app hands you would stop being recognised --
  // the one case this matcher exists for.
  it.each(['Report ]final[.pdf', String.raw`weird\].pdf`, 'plain.pdf', 'accenti àèù.pdf'])(
    'round-trips %s',
    (filename) => {
      const att = { id: 'abc-123', filename, mime_type: 'application/pdf' }
      const parsed = parseAttachmentMarkdownRef(attachmentMarkdownRef(att))
      expect(parsed).not.toBeNull()
      expect(parsed?.label).toBe(filename)
      expect(parsed?.href).toBe('/attachments/abc-123/download')
      expect(parsed?.image).toBe(false)
    },
  )

  it('still refuses anything that is not one whole attachment reference', () => {
    expect(parseAttachmentMarkdownRef('testo [a](/attachments/x/download) coda')).toBeNull()
    expect(parseAttachmentMarkdownRef('[a](https://example.com)')).toBeNull()
    expect(parseAttachmentMarkdownRef('non una reference')).toBeNull()
  })
})

describe('mdLink', () => {
  it('builds both shapes', () => {
    expect(mdLink('a', '/x')).toBe('[a](/x)')
    expect(mdLink('a', '/x', { image: true })).toBe('![a](/x)')
  })
})
