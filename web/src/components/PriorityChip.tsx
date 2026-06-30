// Priority 1..25 (1 = most prioritary, 25 = least; the default task
// ordering is ascending so 1 is always on top). Continuous hue ramp:
// 1 = red (hot / immediate) shifting toward cool blue as it grows.
function priorityHue(priority: number): number {
  const p = Math.max(1, Math.min(25, priority))
  return Math.round(((p - 1) / 24) * 220) // 0 red -> 220 blue
}

export function PriorityChip({
  priority,
  score,
}: {
  priority: number
  score?: number | null
}) {
  const hue = priorityHue(priority)
  // The hue at a fixed lightness drives only the decorative tint/border.
  // The P-number label inherits .chip's themed `--text-h`, so it stays
  // legible in both themes (a single fixed lightness could not). The dot
  // keeps the per-priority hue but at the theme-aware `--prio-l`
  // lightness, so it holds a >=3:1 boundary on the pale/dark chip body.
  const tint = `hsl(${hue} 70% 45%)`
  return (
    <span
      className="chip prio"
      style={{
        background: `color-mix(in srgb, ${tint} 14%, transparent)`,
        borderColor: `color-mix(in srgb, ${tint} 45%, transparent)`,
      }}
      title={
        score != null
          ? `importance x urgency = ${score}`
          : `priority ${priority}`
      }
    >
      <span
        className="chip__dot"
        style={{ background: `hsl(${hue} 70% var(--prio-l))` }}
      />
      P{priority}
    </span>
  )
}
