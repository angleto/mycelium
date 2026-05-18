// Derived priority 1..4 (1 = highest, ADR-0024). Color scale: 1 = red
// (hot/immediate), ascending toward cool/light (per the user's rule).
// Distinct hues (not a single warm ramp) so P1..P4 are clearly
// separable: red, orange, teal, blue. 1 = hottest/immediate.
const COLORS: Record<number, string> = {
  1: '#d11149',
  2: '#e8590c',
  3: '#0d9488',
  4: '#3b6fb6',
}

export function PriorityChip({
  priority,
  score,
}: {
  priority: number
  score?: number | null
}) {
  const c = COLORS[priority] ?? COLORS[4]
  return (
    <span
      className="chip prio"
      style={{ background: `${c}22`, borderColor: `${c}66`, color: c }}
      title={score != null ? `importance x urgency = ${score}` : `priority ${priority}`}
    >
      <span className="chip__dot" style={{ background: c }} />P{priority}
      {score != null ? ` · ${score}` : ''}
    </span>
  )
}
