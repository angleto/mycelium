// Turning an XML document into something a human can READ.
//
// Every FatturaPA this app produces arrives as ONE line. The builder ends in
// ``ET.tostring(...)`` with no ``ET.indent``, and that is deliberate and must
// stay that way: those bytes are the fiscal document. They are transmitted to
// SdI, frozen on the invoice row, stored as a connector's shadow ``dry_run_xml``
// and asserted byte-for-byte by the backend tests. Re-indenting the BUILDER
// would change what gets sent.
//
// So indentation is a VIEW concern, and this module is where it lives: the API
// keeps returning the exact bytes, "Download .xml" keeps handing over the real
// file, and only the rendering is formatted. Nothing here ever feeds a
// serialiser -- the output is a display plan, not a document.
//
// Parsing is done by the browser's own ``DOMParser`` rather than by a regex
// over angle brackets. A hand-rolled tokeniser gets CDATA, entities, comments
// and namespaced names subtly wrong, and "subtly wrong" on a document an
// operator is diffing against another vendor's output is worse than no viewer
// at all.

/** One attribute, in document order. */
export type XmlAttr = { name: string; value: string }

/** One rendered line. ``depth`` is the nesting level, not a character count:
 * the view decides how wide an indent step is. */
export type XmlLine =
  /** ``<?xml ...?>`` and any other processing instruction. */
  | { kind: 'pi'; depth: number; text: string }
  | { kind: 'comment'; depth: number; text: string }
  /** An element with element children: opens here, closes at its ``close``. */
  | { kind: 'open'; depth: number; name: string; attrs: XmlAttr[] }
  | { kind: 'close'; depth: number; name: string }
  /** ``<X/>`` -- no children at all. */
  | { kind: 'empty'; depth: number; name: string; attrs: XmlAttr[] }
  /** ``<X>value</X>`` on one line: how a human reads FatturaPA. */
  | { kind: 'leaf'; depth: number; name: string; attrs: XmlAttr[]; value: string }
  /** Text that could not be folded onto its element's line (mixed content, or
   * a value carrying newlines). Verbatim. */
  | { kind: 'text'; depth: number; text: string }

/** The XML declaration, which ``DOMParser`` does not keep.
 *
 * ``<?xml`` then WHITESPACE, not ``\b``: a word boundary also sits between
 * ``xml`` and the hyphen of ``<?xml-stylesheet``, so ``\b`` swallowed the
 * stylesheet PI here and the DOM then emitted it a second time.
 *
 * Non-greedy, because a pseudo-attribute may legitimately contain a ``?`` (a
 * stylesheet href with a query string) and ``[^?]*`` would refuse those. */
const DECLARATION = /^\s*<\?xml\s[\s\S]*?\?>/

/** Whether whitespace inside this element is content rather than formatting.
 *
 * ``xml:space`` is INHERITED (XML 1.0 §2.10): it is declared once on an
 * ancestor and governs everything under it until a descendant says
 * ``default``. Reading it off one element would honour the declaration only on
 * the element carrying it. */
function preservesSpace(el: Element): boolean {
  for (let node: Element | null = el; node; node = node.parentElement) {
    const declared = node.getAttribute('xml:space')
    if (declared === 'preserve') return true
    if (declared === 'default') return false
  }
  return false
}

/** Is this whitespace the SOURCE's own layout, or content?
 *
 * Indentation always wraps a line, so it always contains a newline. Whitespace
 * that does not is something in the data: a Denominazione with a trailing
 * space is a real defect an operator may be hunting, and a view that trimmed
 * it would render the broken document and the correct one identically. */
function isLayout(raw: string): boolean {
  return raw.includes('\n')
}

function attrsOf(el: Element): XmlAttr[] {
  return Array.from(el.attributes, (a) => ({ name: a.name, value: a.value }))
}

function pushNode(node: Node, depth: number, out: XmlLine[]): void {
  if (node.nodeType === Node.COMMENT_NODE) {
    out.push({ kind: 'comment', depth, text: node.nodeValue ?? '' })
    return
  }
  if (node.nodeType === Node.PROCESSING_INSTRUCTION_NODE) {
    const pi = node as ProcessingInstruction
    out.push({ kind: 'pi', depth, text: `<?${pi.target} ${pi.data}?>` })
    return
  }
  if (node.nodeType === Node.TEXT_NODE || node.nodeType === Node.CDATA_SECTION_NODE) {
    const raw = node.nodeValue ?? ''
    // Whitespace BETWEEN elements is the source document's own indentation and
    // reprinting it under ours would double it -- but only the kind that wraps
    // a line is indentation. See ``isLayout``.
    if (!raw.trim() && isLayout(raw)) return
    for (const line of raw.split('\n')) out.push({ kind: 'text', depth, text: line })
    return
  }
  if (node.nodeType !== Node.ELEMENT_NODE) return

  const el = node as Element
  const attrs = attrsOf(el)
  // ``nodeName``, not ``localName``: the prefix (``p:FatturaElettronica``,
  // ``ds:Signature``) is part of what is IN the file, and this view answers
  // "what did we send".
  const name = el.nodeName
  const kids = Array.from(el.childNodes).filter(
    (n) =>
      n.nodeType === Node.ELEMENT_NODE ||
      n.nodeType === Node.COMMENT_NODE ||
      n.nodeType === Node.PROCESSING_INSTRUCTION_NODE ||
      ((n.nodeType === Node.TEXT_NODE || n.nodeType === Node.CDATA_SECTION_NODE) &&
        (preservesSpace(el) ||
          !!(n.nodeValue ?? '').trim() ||
          !isLayout(n.nodeValue ?? ''))),
  )

  if (kids.length === 0) {
    // ``<b/>`` and ``<b>   </b>`` are different documents, and the difference
    // is one an operator chasing a wrongly-filled field has to be able to
    // see. Only an element with NO child nodes at all is self-closing.
    if (el.childNodes.length > 0) {
      out.push({ kind: 'leaf', depth, name, attrs, value: '' })
      return
    }
    out.push({ kind: 'empty', depth, name, attrs })
    return
  }
  const onlyText =
    kids.length === 1 &&
    (kids[0].nodeType === Node.TEXT_NODE || kids[0].nodeType === Node.CDATA_SECTION_NODE)
  if (onlyText) {
    const raw = kids[0].nodeValue ?? ''
    const value = preservesSpace(el) || !isLayout(raw) ? raw : raw.trim()
    // A single-line value folds onto the element's own line. A value carrying
    // newlines does not: squashing it would change what the document says.
    if (!value.includes('\n')) {
      out.push({ kind: 'leaf', depth, name, attrs, value })
      return
    }
  }
  out.push({ kind: 'open', depth, name, attrs })
  for (const kid of kids) pushNode(kid, depth + 1, out)
  out.push({ kind: 'close', depth, name })
}

/**
 * A display plan for ``source``, or null when it is not well-formed XML.
 *
 * Null is a real answer, not a failure to hide: the caller shows the raw text
 * instead and says so. A document that will not parse is exactly the one an
 * operator most needs to look at.
 */
export function parseXml(source: string): XmlLine[] | null {
  if (!source.trim()) return null
  let doc: Document
  try {
    doc = new DOMParser().parseFromString(source, 'application/xml')
  } catch {
    return null
  }
  // How every browser reports a malformed document: it returns a document
  // containing a ``parsererror`` element rather than throwing. Namespaced in
  // Firefox, bare in Chromium, so the tag name is the portable check. A
  // FatturaPA or an SdI notification never contains an element by that name.
  if (doc.getElementsByTagName('parsererror').length > 0) return null
  if (!doc.documentElement) return null

  const out: XmlLine[] = []
  const declaration = DECLARATION.exec(source)
  if (declaration) out.push({ kind: 'pi', depth: 0, text: declaration[0].trim() })
  // Top level first: a stylesheet PI or a licence comment sits OUTSIDE the
  // root element and would be invisible if the walk started at the root.
  for (const node of Array.from(doc.childNodes)) pushNode(node, 0, out)
  return out
}

/** The text of one line, without indentation. Used by the tests and by nothing
 * else: the view renders the parts separately so it can colour them. */
export function lineText(line: XmlLine): string {
  const attrs = (as: XmlAttr[]) => as.map((a) => ` ${a.name}="${a.value}"`).join('')
  switch (line.kind) {
    case 'pi':
      return line.text
    case 'comment':
      return `<!--${line.text}-->`
    case 'open':
      return `<${line.name}${attrs(line.attrs)}>`
    case 'close':
      return `</${line.name}>`
    case 'empty':
      return `<${line.name}${attrs(line.attrs)}/>`
    case 'leaf':
      return `<${line.name}${attrs(line.attrs)}>${line.value}</${line.name}>`
    case 'text':
      return line.text
  }
}
