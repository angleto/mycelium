// Priority 1..25 (1 = most prioritary, 25 = least; the default task
// ordering is ascending so 1 is always on top). Continuous hue ramp:
// 1 = red (hot / immediate) shifting toward cool blue as it grows.
function color(priority: number): string {
  const p = Math.max(1, Math.min(25, priority))
  const hue = Math.round(((p - 1) / 24) * 220) // 0 red -> 220 blue
  return `hsl(${hue} 70% 45%)`
}

export function PriorityChip({
  priority,
  score,
}: {
  priority: number
  score?: number | null
}) {
  const c = color(priority)
  return (
    <span
      className="chip prio"
      style={{
        background: `color-mix(in srgb, ${c} 14%, transparent)`,
        borderColor: `color-mix(in srgb, ${c} 45%, transparent)`,
        color: c,
      }}
      title={
        score != null
          ? `importance x urgency = ${score}`
          : `priority ${priority}`
      }
    >
      <span className="chip__dot" style={{ background: c }} />P{priority}
    </span>
  )
}
