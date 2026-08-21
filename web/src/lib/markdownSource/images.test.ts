import { afterEach, describe, expect, it } from 'vitest'
import { EditorState } from '@codemirror/state'
import { EditorView } from '@codemirror/view'
import { forceParsing } from '@codemirror/language'
import { markdownSourceExtensions } from './extensions'
import { parseImageEmbed } from './widgets'
import { attachmentAuthPaths } from './attachmentRetain'

// Image embeds: parsed out of the source, replaced by the picture, and their
// bytes held for the editor's lifetime so the caret cannot cause a
// re-download.

const views: EditorView[] = []

function open(src: string): EditorView {
  const view = new EditorView({
    state: EditorState.create({
      doc: src,
      extensions: markdownSourceExtensions({ src, onChange: () => {} }),
    }),
  })
  forceParsing(view, src.length, 5_000)
  views.push(view)
  return view
}

function lines(view: EditorView): string {
  return Array.from(view.contentDOM.querySelectorAll('.cm-line'))
    .map((el) => el.textContent ?? '')
    .join('\n')
}

afterEach(() => {
  while (views.length) views.pop()?.destroy()
})

describe('parseImageEmbed', () => {
  it('reads a plain embed', () => {
    expect(parseImageEmbed('![alt](/attachments/x/download)')).toEqual({
      src: '/attachments/x/download',
      alt: 'alt',
      title: undefined,
    })
  })

  it('reads a title, in either quote style', () => {
    expect(parseImageEmbed('![a](x.png "T")')?.title).toBe('T')
    expect(parseImageEmbed("![a](x.png 'T')")?.title).toBe('T')
  })

  it('unescapes a bracketed alt, the form the app now emits', () => {
    // attachmentMarkdownRef escapes `]` in a filename; the preview has to
    // read back what the emitter writes, or an image whose name contains a
    // bracket would silently stop rendering.
    const parsed = parseImageEmbed(String.raw`![Report \]final\[.png](/attachments/x/download)`)
    expect(parsed?.alt).toBe('Report ]final[.png')
  })

  it('unwraps an angle-bracketed destination', () => {
    expect(parseImageEmbed('![a](</a b/c.png>)')?.src).toBe('/a b/c.png')
  })

  it('refuses anything that is not exactly one embed', () => {
    expect(parseImageEmbed('testo ![a](x.png)')).toBeNull()
    expect(parseImageEmbed('[a](x.png)')).toBeNull()
    expect(parseImageEmbed('![a](x.png) coda')).toBeNull()
  })
})

describe('the image widget', () => {
  it('replaces the embed off the caret line, and never touches the document', () => {
    const src = 'prima\n\n![grafico](/attachments/abc-123/download)\n\ndopo\n'
    const view = open(src)
    view.dispatch({ selection: { anchor: 0 } })
    expect(view.contentDOM.querySelectorAll('.cm-md-widget-inline')).toHaveLength(1)
    expect(lines(view)).not.toContain('/attachments/abc-123/download')
    expect(view.state.sliceDoc()).toBe(src)
  })

  it('gives the source back with the caret on the line', () => {
    const src = 'prima\n\n![grafico](/attachments/abc-123/download)\n\ndopo\n'
    const view = open(src)
    view.dispatch({ selection: { anchor: src.indexOf('grafico') } })
    expect(view.contentDOM.querySelectorAll('.cm-md-widget-inline')).toHaveLength(0)
    expect(lines(view)).toContain('![grafico](/attachments/abc-123/download)')
    expect(view.state.sliceDoc()).toBe(src)
  })

  it('leaves a LINK to an attachment alone; only an embed becomes a picture', () => {
    const src = '[doc.pdf](/attachments/abc-123/download)\n'
    const view = open(src)
    view.dispatch({ selection: { anchor: src.length } })
    expect(view.contentDOM.querySelectorAll('.cm-md-widget-inline')).toHaveLength(0)
    expect(view.state.sliceDoc()).toBe(src)
  })
})

describe('what the editor retains', () => {
  it('collects every auth path the body embeds', () => {
    const doc = [
      '![a](/attachments/one/download)',
      'testo ![b](/attachments/two/download) in mezzo',
      '[non un embed](/attachments/three/download)',
      '![assoluto](https://example.com/x.png)',
    ].join('\n\n')
    expect(attachmentAuthPaths(doc, undefined).sort()).toEqual([
      '/attachments/one/download',
      '/attachments/two/download',
    ])
  })

  it('deduplicates the same attachment embedded twice', () => {
    const doc = '![a](/attachments/one/download)\n\n![ancora](/attachments/one/download)\n'
    expect(attachmentAuthPaths(doc, undefined)).toEqual(['/attachments/one/download'])
  })

  it('skips a bare filename when there is no parent to resolve it against', () => {
    // Not retained rather than guessed: missing a retain costs a
    // re-download, inventing a path would poison a process-wide cache.
    expect(attachmentAuthPaths('![f](Fig02.png)\n', undefined)).toEqual([])
  })

  it('reads an angle-bracketed and an escaped destination', () => {
    const doc = '![a](</attachments/one/download>)\n'
    expect(attachmentAuthPaths(doc, undefined)).toEqual(['/attachments/one/download'])
  })
})
