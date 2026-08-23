import { describe, expect, it } from 'vitest'
import { lineText, parseXml, type XmlLine } from './xmlDoc'

// The document under test is the shape this app actually produces: one line,
// no whitespace between elements, an XML declaration the DOM drops, and a
// namespace prefix on the root.
const FATTURA =
  `<?xml version='1.0' encoding='utf-8'?>\n` +
  `<p:FatturaElettronica xmlns:p="http://ivaservizi.agenziaentrate.gov.it/docs/xsd/fatture/v1.2"` +
  ` versione="FPR12"><FatturaElettronicaHeader><DatiTrasmissione>` +
  `<ProgressivoInvio>ANTEPRIMA</ProgressivoInvio><CodiceDestinatario>0000000</CodiceDestinatario>` +
  `</DatiTrasmissione></FatturaElettronicaHeader></p:FatturaElettronica>`

function render(source: string): string[] {
  const lines = parseXml(source)
  if (!lines) throw new Error('expected a parse')
  return lines.map((l) => '  '.repeat(l.depth) + lineText(l))
}

describe('parseXml', () => {
  it('unfolds the one-line document the builder emits', () => {
    expect(render(FATTURA)).toEqual([
      `<?xml version='1.0' encoding='utf-8'?>`,
      `<p:FatturaElettronica xmlns:p="http://ivaservizi.agenziaentrate.gov.it/docs/xsd/fatture/v1.2" versione="FPR12">`,
      '  <FatturaElettronicaHeader>',
      '    <DatiTrasmissione>',
      '      <ProgressivoInvio>ANTEPRIMA</ProgressivoInvio>',
      '      <CodiceDestinatario>0000000</CodiceDestinatario>',
      '    </DatiTrasmissione>',
      '  </FatturaElettronicaHeader>',
      '</p:FatturaElettronica>',
    ])
  })

  it('keeps the declaration, which DOMParser throws away', () => {
    const lines = parseXml(FATTURA) as XmlLine[]
    expect(lines[0]).toEqual({ kind: 'pi', depth: 0, text: `<?xml version='1.0' encoding='utf-8'?>` })
    // ...and does not invent one when the source has none.
    expect((parseXml('<a>1</a>') as XmlLine[])[0].kind).toBe('leaf')
  })

  it('keeps the namespace prefix and the attribute order of the file', () => {
    const lines = parseXml(FATTURA) as XmlLine[]
    const root = lines.find((l) => l.kind === 'open')
    expect(root).toMatchObject({
      name: 'p:FatturaElettronica',
      attrs: [{ name: 'xmlns:p' }, { name: 'versione', value: 'FPR12' }],
    })
  })

  it('does not re-indent a document that was already indented', () => {
    // An SdI notification arrives pretty-printed. Its own indentation is
    // whitespace BETWEEN elements: reprinting it under ours would double it.
    const pretty = '<a>\n  <b>\n    <c>1</c>\n  </b>\n</a>'
    expect(render(pretty)).toEqual(['<a>', '  <b>', '    <c>1</c>', '  </b>', '</a>'])
  })

  it('folds a single-line value onto its element and never a multi-line one', () => {
    expect(render('<a><b>ACME</b></a>')).toEqual(['<a>', '  <b>ACME</b>', '</a>'])
    // Squashing this onto one line would change what the document says.
    expect(render('<a><b>one\ntwo</b></a>')).toEqual(['<a>', '  <b>', '    one', '    two', '  </b>', '</a>'])
  })

  it('distinguishes the source\'s layout from whitespace that is content', () => {
    // Regression guard. Indentation always wraps a line, so it always carries
    // a newline; whitespace that does not is IN the data. A "Denominazione"
    // with a trailing space is a real defect, and a view that trimmed it would
    // render the broken document and the correct one identically -- which is
    // exactly what an operator diffing two vendors' XML is looking for.
    expect(render('<a><b>ACME SRL </b></a>')).toEqual(['<a>', '  <b>ACME SRL </b>', '</a>'])
    expect(render('<a><b>   </b></a>')).toEqual(['<a>', '  <b>   </b>', '</a>'])
    expect(render('<a><b>\tX</b></a>')).toEqual(['<a>', '  <b>\tX</b>', '</a>'])
    // ...while the source's own indentation is dropped, not reprinted.
    expect(render('<a>\n  <b>\n    ACME\n  </b>\n</a>')).toEqual(['<a>', '  <b>ACME</b>', '</a>'])
    expect(render('<a>\n</a>')).toEqual(['<a></a>'])
  })

  it('honours xml:space on an ancestor, which is where it is declared', () => {
    // XML 1.0 s2.10: xml:space is INHERITED. Reading it off one element would
    // honour the declaration only on the element carrying it.
    expect(render('<a xml:space="preserve"><b>\n  x\n</b></a>')).toEqual([
      '<a xml:space="preserve">',
      '  <b>',
      '    ',
      '      x',
      '    ',
      '  </b>',
      '</a>',
    ])
    // ...and a descendant can turn it back off.
    expect(render('<a xml:space="preserve"><b xml:space="default">\n  x\n</b></a>')).toEqual([
      '<a xml:space="preserve">',
      '  <b xml:space="default">x</b>',
      '</a>',
    ])
  })

  it('shows an empty element as empty, not as a value of nothing', () => {
    expect(render('<a><b/></a>')).toEqual(['<a>', '  <b/>', '</a>'])
  })

  it('keeps comments and processing instructions, including outside the root', () => {
    const src = '<?xml-stylesheet href="a.xsl?v=2"?><!-- top --><a><!-- in --><b>1</b></a>'
    expect(render(src)).toEqual([
      '<?xml-stylesheet href="a.xsl?v=2"?>',
      '<!-- top -->',
      '<a>',
      '  <!-- in -->',
      '  <b>1</b>',
      '</a>',
    ])
  })

  it('keeps mixed content in order', () => {
    expect(render('<a>before<b>1</b>after</a>')).toEqual([
      '<a>',
      '  before',
      '  <b>1</b>',
      '  after',
      '</a>',
    ])
  })

  it('reads CDATA as the text it stands for', () => {
    expect(render('<a><![CDATA[<not a tag>]]></a>')).toEqual(['<a><not a tag></a>'])
  })

  it('returns null rather than a half-parsed document', () => {
    // Each of these is a document an operator most needs to look at, so the
    // caller shows the raw text and says why. Guessing would be worse.
    for (const bad of ['', '   ', 'not xml at all', '<a><b></a>', '<a>']) {
      expect(parseXml(bad)).toBeNull()
    }
  })

  it('does not need escaping to be undone by hand', () => {
    // The DOM resolves entities, and the view renders text as text: an XML
    // value carrying markup can never become markup on the page.
    const lines = parseXml('<a>&lt;script&gt;alert(1)&lt;/script&gt;</a>') as XmlLine[]
    expect(lines[0]).toMatchObject({ kind: 'leaf', value: '<script>alert(1)</script>' })
  })
})
