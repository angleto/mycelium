import { describe, expect, it } from 'vitest'
import { Text } from '@codemirror/state'
import { scanBlocks, type ScannedBlock } from './blockScan'

// The scanner decides where block widgets go, and a widget declares its own
// height, so a wrong extent is not a cosmetic bug: it is the document
// jumping under the reader. These are pure-function tests over the line
// text, no editor involved.

function scan(src: string): ScannedBlock[] {
  return scanBlocks(Text.of(src.split('\n')))
}

function slice(src: string, b: ScannedBlock): string {
  return src.slice(b.from, b.to)
}

describe('fences', () => {
  it('finds a closed backtick fence with its info string', () => {
    const src = 'prima\n\n```js\nlet x = 1\n```\n\ndopo\n'
    const [b] = scan(src)
    expect(b.kind).toBe('fence')
    expect(slice(src, b)).toBe('```js\nlet x = 1\n```')
    if (b.kind === 'fence') {
      expect(b.info).toBe('js')
      expect(src.slice(b.contentFrom, b.contentTo)).toBe('let x = 1')
    }
  })

  it('finds a tilde fence, and a longer closing run', () => {
    const src = '~~~\ncode\n~~~\n'
    const [b] = scan(src)
    expect(b.kind).toBe('fence')
    expect(slice(src, b)).toBe('~~~\ncode\n~~~')
  })

  it('a longer fence contains a shorter one', () => {
    // The inner ``` is content, not a close. Getting this wrong splits the
    // document -- the exact corruption the retired serializer produced.
    const src = '````\n```\ninner\n```\n````\n'
    const blocks = scan(src)
    expect(blocks).toHaveLength(1)
    expect(slice(src, blocks[0])).toBe('````\n```\ninner\n```\n````')
  })

  it('an unclosed fence runs to the end of the document', () => {
    const src = '```\nmai chiuso\nancora\n'
    const [b] = scan(src)
    expect(slice(src, b)).toBe('```\nmai chiuso\nancora\n')
  })

  it('does not open a fence on an inline code span', () => {
    const src = 'usa `pnpm test` e poi `x`\n'
    expect(scan(src)).toEqual([])
  })

  it('shadows everything inside it', () => {
    // A `$$`, a table and a setext underline inside a fence are literal text.
    const src = '```\n$$\nx\n$$\n\n| a | b |\n| --- | --- |\n\nTitolo\n======\n```\n'
    const blocks = scan(src)
    expect(blocks).toHaveLength(1)
    expect(blocks[0].kind).toBe('fence')
  })
})

describe('math blocks', () => {
  it('finds a multi-line $$ block and its tex', () => {
    const src = 'prima\n\n$$\n\\sum_{i=0}^{n} x_i\n$$\n\ndopo\n'
    const [b] = scan(src)
    expect(b.kind).toBe('math')
    expect(slice(src, b)).toBe('$$\n\\sum_{i=0}^{n} x_i\n$$')
    if (b.kind === 'math') expect(b.tex).toBe('\\sum_{i=0}^{n} x_i')
  })

  it('finds the single-line form', () => {
    const src = '$$ x^2 $$\n'
    const [b] = scan(src)
    expect(b.kind).toBe('math')
    if (b.kind === 'math') expect(b.tex).toBe('x^2')
  })

  it('ignores an unclosed $$', () => {
    expect(scan('$$\nmai chiuso\n')).toEqual([])
  })
})

describe('tables', () => {
  it('needs a delimiter row on the next line', () => {
    expect(scan('| a | b |\ntesto\n')).toEqual([])
  })

  it('spans header, delimiter and every consecutive row', () => {
    const src = 'prima\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n| 3 | 4 |\n\ndopo\n'
    const [b] = scan(src)
    expect(b.kind).toBe('table')
    expect(slice(src, b)).toBe('| a | b |\n| --- | --- |\n| 1 | 2 |\n| 3 | 4 |')
  })

  it('accepts padded and aligned delimiter rows', () => {
    for (const delim of ['|---|---|', '| :-- | --: |', '| :-: | --- |', '--- | ---']) {
      const src = `| a | b |\n${delim}\n| 1 | 2 |\n`
      const [b] = scan(src)
      expect(b?.kind, delim).toBe('table')
    }
  })
})

describe('setext headings', () => {
  it('finds an = underline after a paragraph line', () => {
    const src = 'Titolo\n======\n\ntesto\n'
    const [b] = scan(src)
    expect(b.kind).toBe('setext')
    if (b.kind === 'setext') {
      expect(b.level).toBe(1)
      expect(src.slice(b.underlineFrom, b.to)).toBe('======')
    }
  })

  it('finds a - underline, which CommonMark reads as an h2 not a rule', () => {
    const src = 'Sottotitolo\n-----------\n\ntesto\n'
    const [b] = scan(src)
    expect(b.kind).toBe('setext')
    if (b.kind === 'setext') expect(b.level).toBe(2)
  })

  it('does not fire on a thematic break after a blank line', () => {
    expect(scan('testo\n\n---\n\naltro\n')).toEqual([])
  })

  it('does not fire on a table delimiter row', () => {
    const blocks = scan('| a | b |\n| --- | --- |\n| 1 | 2 |\n')
    expect(blocks.map((b) => b.kind)).toEqual(['table'])
  })

  it('does not fire inside a blockquote, where the line is not all dashes', () => {
    expect(scan('> testo\n> ---\n')).toEqual([])
  })
})

describe('the whole corpus scans without overlap', () => {
  const fixtures = import.meta.glob('../../../test/markdown-corpus/*.md', {
    query: '?raw',
    import: 'default',
    eager: true,
  }) as Record<string, string>

  it.each(Object.entries(fixtures).map(([p, src]) => [p.slice(p.lastIndexOf('/') + 1), src]))(
    '%s',
    (_name, src) => {
      const blocks = scan(src)
      // Ordered, non-overlapping, inside the document. A widget built from
      // an overlapping range makes CodeMirror throw at render time.
      let prev = -1
      for (const b of blocks) {
        expect(b.from).toBeGreaterThanOrEqual(prev)
        expect(b.to).toBeGreaterThanOrEqual(b.from)
        expect(b.to).toBeLessThanOrEqual(src.length)
        prev = b.to
      }
    },
  )
})
