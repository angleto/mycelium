// Which hrefs the rich editor keeps as a real ``link`` mark.
//
// This is the ONE policy point. tiptap v3 consults ``isAllowedUri`` when
// parsing an ``<a href>`` back into a mark, when rendering it, and from the
// setLink command. An href it rejects is not "kept as plain text with a
// warning": the mark is silently dropped and the destination is gone on the
// next save. So anything this predicate gets wrong is data loss.
//
// tiptap's own default rejects any relative href containing a directory
// separator. Its regex ends in ``[^a-z+.-:]``, where ``.-:`` reads as a
// RANGE (0x2E-0x3A) that contains ``/`` -- so ``README.md`` passes while
// ``docs/00-overview.md`` and ``sources/`` do not. That is how a verbatim
// markdown document lost every path in its reading-order table the first
// time the note was opened in the SPA.
//
// The ``validate`` option that used to carry this policy in the editor is
// NOT this gate in v3: it is aliased onto ``shouldAutoLink``, which only
// feeds the autolink plugin. It was dead code, and tiptap's default
// governed the round-trip instead.
//
// This also gates renderHTML, so it must stay an XSS gate: every scripting
// scheme is rejected, and so is a protocol-relative ``//host`` (an off-site
// absolute URL wearing a relative costume).

/** Mention DSL emitted by the @-typeahead, e.g. ``@note:<uuid>``. */
const MENTION = /^@(?:task|note|tag):/i
/** Schemes that carry no script and are ordinary links. */
const SAFE_SCHEME = /^(?:https?|mailto|tel|callto|sms|xmpp|ftp|ftps)$/i
const SCHEME = /^([A-Za-z][A-Za-z0-9+.-]*):/

export function isEditorHref(url: string): boolean {
  if (!url) return false
  // Drop C0 controls and spaces before matching, the way tiptap does, so a
  // ``java\nscript:`` smuggle cannot slip past the scheme test below.
  const u = Array.from(url)
    .filter((ch) => ch.charCodeAt(0) > 0x20)
    .join('')
  if (!u || u.startsWith('//')) return false
  if (MENTION.test(u)) return true
  const scheme = SCHEME.exec(u)
  if (scheme) return SAFE_SCHEME.test(scheme[1])
  // Scheme-less: a same-document anchor (``#sezione``), an absolute path
  // (``/attachments/<id>/download``), or a relative one (``sources/``,
  // ``../code/``, ``00-overview.md``). Whitespace is the only thing that
  // cannot survive an unbracketed markdown destination.
  return !/\s/.test(u)
}
