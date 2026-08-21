// Building markdown by string interpolation, safely.
//
// Every place the app writes a link or an image reference into a body does
// it by interpolation: `[${label}](${href})`. The label is user data (an
// uploaded filename, a task title, a note title), and nothing was escaping
// it. An attachment called `Report ]final.pdf` produced
//
//     [Report ]final.pdf](/attachments/<id>/download)
//
// which is not a link: it parses as the link `[Report ]` followed by the
// literal text `final.pdf](/attachments/…)`. The backend's filename
// sanitiser only strips path separators and leading dots, so `]`, `[` and
// `\` all reach here intact.
//
// This module is the single escaping rule for the emitting side. The
// matcher that recovers such a reference from a paste
// (`parseAttachmentMarkdownRef`) understands the escaped form, so the two
// stay symmetric.
//
// Mirrored, deliberately duplicated, in two other emitters that cannot
// share this code: `mycelium_core.markdown_inline` (used by the MCP
// `attach_file` tools) and `mycelium_cli.cmds._common` (the CLI ships
// standalone, with no dependency on core). Change one, change all three.

/**
 * Escape a string for use as a markdown link/image label, i.e. the text
 * between `[` and `]`.
 *
 * `\` first (so the escapes we then add are not themselves re-escaped),
 * then the brackets. Newlines collapse to a single space: a label may
 * legally wrap in CommonMark, but a blank line inside it ends the
 * paragraph and truncates the link, and every reference this app emits is
 * a one-liner anyway.
 */
export function mdLinkLabel(text: string): string {
  return text
    .replace(/\\/g, '\\\\')
    .replace(/([[\]])/g, '\\$1')
    .replace(/\s*[\r\n]+\s*/g, ' ')
}

/**
 * Escape a string for use as a markdown link/image destination, i.e. the
 * text between `(` and `)`.
 *
 * A destination containing whitespace or parentheses has to be wrapped in
 * angle brackets, and then `<`, `>` and `\` need escaping inside the
 * wrapper. Every destination this app emits today
 * (`/attachments/<id>/download`, `@task:<uuid>`) is already safe, so this
 * is defence in depth rather than a live fix: it stops a future caller
 * from silently emitting a broken link.
 */
export function mdLinkDestination(href: string): string {
  if (!/[\s()<>\\]/.test(href)) return href
  return `<${href.replace(/([\\<>])/g, '\\$1')}>`
}

/** `[label](href)`, or `![label](href)` when `image`. Both parts escaped. */
export function mdLink(
  label: string,
  href: string,
  opts?: { image?: boolean },
): string {
  return `${opts?.image ? '!' : ''}[${mdLinkLabel(label)}](${mdLinkDestination(href)})`
}

/**
 * Undo `mdLinkLabel`'s backslash escapes, for the matchers that recover a
 * label out of markdown text. Any `\x` becomes `x`, which is exactly
 * CommonMark's rule for a backslash escape inside a label.
 */
export function mdUnescapeLabel(label: string): string {
  return label.replace(/\\([\s\S])/g, '$1')
}
