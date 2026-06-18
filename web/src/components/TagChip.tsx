import { kindGlyph } from '../lib/tagGlyph'

// A tag rendered as a colored chip. The leading glyph encodes the
// tag's kind so that two tags with the same name (e.g. project
// "Analisi" vs. client "Analisi") are distinguishable at a glance —
// see ../lib/tagGlyph for the kind -> shape mapping. The glyph is
// colored with the tag's color (falls back to the accent).
//
// ``color`` is a hex string or null; ``kind`` is one of the TagKind
// enum values. Shared across tasks/notes/tag manager.
export function TagChip({
  name,
  color,
  kind,
}: {
  name: string
  color?: string | null
  kind?: string
}) {
  // The kind glyph keeps the tag's hue, but clamped toward the per-theme
  // text color so an extreme user color (near-white or near-black) can
  // never collapse into the chip's same-hue tint background. The chip's
  // bg tint + border already carry the raw hue.
  const glyphColor = color
    ? `color-mix(in srgb, ${color} 55%, var(--text-h))`
    : 'var(--accent)'
  const glyph = kindGlyph(kind)
  return (
    <span
      className="chip"
      style={{
        background: color ? `${color}22` : 'var(--surface-2)',
        borderColor: color ? `${color}66` : 'var(--border)',
      }}
      title={kind ? `${kind}: ${name}` : name}
    >
      <span
        className="chip__glyph"
        style={{ color: glyphColor }}
        aria-hidden="true"
      >
        {glyph}
      </span>
      {name}
    </span>
  )
}

