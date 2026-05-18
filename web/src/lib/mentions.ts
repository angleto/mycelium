// Cross-reference DSL, same shape as bitvision: markdown links whose
// href is `@<kind>:<uuid>`, e.g. [Fix duct](@task:9cf6bc7f-...). Stored
// as plain markdown so it round-trips; resolved at render time into a
// router link / chip. Flow kinds: task, note, tag.

export type MentionKind = 'task' | 'note' | 'tag'

const UUID = '[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
const HREF_RE = new RegExp(`^@(task|note|tag):(${UUID})$`)

export function parseMentionHref(
  href: string,
): { kind: MentionKind; id: string } | null {
  const m = HREF_RE.exec(href.trim())
  return m ? { kind: m[1] as MentionKind, id: m[2] } : null
}

export function formatMentionHref(kind: MentionKind, id: string): string {
  return `@${kind}:${id}`
}

export function mentionLink(kind: MentionKind, id: string, label: string): string {
  return `[${label}](${formatMentionHref(kind, id)})`
}

export function routeForMention(kind: MentionKind, id: string): string {
  if (kind === 'task') return `/tasks/${id}`
  if (kind === 'tag') return `/notes?tag=${id}`
  // Opens the note modal (view + edit) in the Notes view.
  return `/notes?open=${id}`
}
