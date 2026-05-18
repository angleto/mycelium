// A tag rendered as a colored chip. color is a hex string or null
// (fall back to the accent). Shared across tasks/notes/tag manager.
export function TagChip({
  name,
  color,
  kind,
}: {
  name: string
  color?: string | null
  kind?: string
}) {
  const c = color || 'var(--accent)'
  return (
    <span
      className="chip"
      style={{
        background: color ? `${color}22` : 'var(--surface-2)',
        borderColor: color ? `${color}66` : 'var(--border)',
      }}
      title={kind ? `${kind}: ${name}` : name}
    >
      <span className="chip__dot" style={{ background: c }} />
      {name}
    </span>
  )
}
