import { useMemo, useState, type CSSProperties } from 'react'
import { useTranslation } from 'react-i18next'
import { parseXml, type XmlAttr, type XmlLine } from '../lib/xmlDoc'

// The one XML reader in the app. Three surfaces need it -- the invoice
// document panel, the SdI notification it also hosts, and a payment
// connector's shadow document -- and before this they had one raw <pre>
// between them plus a download-only button.
//
// It renders REACT ELEMENTS built from the parsed document, never
// dangerouslySetInnerHTML. That is not caution for its own sake: the text in
// these documents is a counterpart's own data (a ragione sociale, a causale)
// and an SdI notification is a file a third party sent us. Building nodes
// means the escaping is React's problem and cannot be got wrong, and it is
// also what makes the colouring possible at all.
//
// What is shown is a RENDERING. Copy and Download hand over the source bytes,
// because those are the document -- see lib/xmlDoc.ts for why the bytes are
// never re-indented.

/** Above this, the browser is asked to lay out more lines than anybody reads.
 * A FatturaPA never comes close; a pathological attachment might. Nothing is
 * dropped silently: the view says what it did not draw. */
const MAX_LINES = 4000

function Attrs({ attrs }: { attrs: XmlAttr[] }) {
  return (
    <>
      {attrs.map((a) => (
        <span key={a.name}>
          {' '}
          <span className="xmlv__attr">{a.name}</span>
          <span className="xmlv__punct">=</span>
          <span className="xmlv__val">&quot;{a.value}&quot;</span>
        </span>
      ))}
    </>
  )
}

function Line({ line }: { line: XmlLine }) {
  const punct = (s: string) => <span className="xmlv__punct">{s}</span>
  const tag = (s: string) => <span className="xmlv__tag">{s}</span>
  switch (line.kind) {
    case 'pi':
      return <span className="xmlv__pi">{line.text}</span>
    case 'comment':
      return <span className="xmlv__comment">&lt;!--{line.text}--&gt;</span>
    case 'open':
      return (
        <>
          {punct('<')}
          {tag(line.name)}
          <Attrs attrs={line.attrs} />
          {punct('>')}
        </>
      )
    case 'close':
      return (
        <>
          {punct('</')}
          {tag(line.name)}
          {punct('>')}
        </>
      )
    case 'empty':
      return (
        <>
          {punct('<')}
          {tag(line.name)}
          <Attrs attrs={line.attrs} />
          {punct('/>')}
        </>
      )
    case 'leaf':
      return (
        <>
          {punct('<')}
          {tag(line.name)}
          <Attrs attrs={line.attrs} />
          {punct('>')}
          <span className="xmlv__text">{line.value}</span>
          {punct('</')}
          {tag(line.name)}
          {punct('>')}
        </>
      )
    case 'text':
      return <span className="xmlv__text">{line.text}</span>
  }
}

/**
 * ``source`` rendered as readable XML, or verbatim when it will not parse.
 *
 * A document that is not well-formed is exactly the one worth looking at, so
 * it is shown as it arrived, with a line saying why it is not formatted --
 * never an error in place of the content.
 */
export function XmlView({ source, label }: { source: string; label: string }) {
  const { t } = useTranslation()
  const lines = useMemo(() => parseXml(source), [source])

  if (!lines) {
    return (
      <>
        <p className="hint">{t('xmlView.unparsed')}</p>
        {/* tabIndex: a scrollable region has to be reachable without a
            pointer, or its content is unreadable by keyboard. */}
        <pre className="xmlv xmlv--raw" tabIndex={0} role="region" aria-label={label}>
          {source}
        </pre>
      </>
    )
  }

  const shown = lines.slice(0, MAX_LINES)
  return (
    <>
      <pre className="xmlv" tabIndex={0} role="region" aria-label={label}>
        {shown.map((line, i) => (
          <div
            key={i}
            className="xmlv__l"
            // Depth only; the geometry is CSS's. An inline ``padding-left``
            // here OVERRODE the rule that pays for the hanging indent, so at
            // depth 0 the negative text-indent had nothing to come out of and
            // pulled the leading ``<`` outside the box, where a container
            // without its own padding (a modal) clipped it.
            style={{ '--xmlv-d': line.depth } as CSSProperties}
          >
            <Line line={line} />
          </div>
        ))}
      </pre>
      {lines.length > shown.length && (
        <p className="hint">
          {t('xmlView.truncated', { shown: shown.length, total: lines.length })}
        </p>
      )}
    </>
  )
}

/** Put ``source`` on the clipboard: the BYTES, not the rendering above.
 *
 * Split out so every surface that hosts the viewer gets the same button with
 * the same "OK" acknowledgement, and so the distinction between the document
 * and its rendering is made in one place. */
export function CopyXmlButton({ source }: { source: string }) {
  const { t } = useTranslation()
  const [done, setDone] = useState(false)
  // ``navigator.clipboard`` is UNDEFINED outside a secure context -- plain
  // http on anything but localhost, which is how this app is reached over a
  // LAN. Reading ``.writeText`` off it throws SYNCHRONOUSLY, before there is a
  // promise for a ``.catch`` to hold, so no amount of chaining catches it: the
  // click handler just raises. Absent the capability the button is not
  // rendered at all rather than rendered dead -- the document is on screen and
  // selectable, and a control that does nothing is worse than no control.
  if (typeof navigator === 'undefined' || !navigator.clipboard) return null
  return (
    <button
      type="button"
      className="btn--sm btn--ghost"
      onClick={() => {
        void navigator.clipboard
          .writeText(source)
          .then(() => {
            setDone(true)
            window.setTimeout(() => setDone(false), 1500)
          })
          // A clipboard the browser refuses at WRITE time (a denied
          // permission, a document that is not focused) is not worth an error
          // banner over the document the user is reading.
          .catch(() => undefined)
      }}
    >
      {done ? t('xmlView.copied') : t('xmlView.copy')}
    </button>
  )
}
