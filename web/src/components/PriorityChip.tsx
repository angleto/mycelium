// Derived priority 1..4 (1 = highest, ADR-0024). Color scale: 1 = red
// (hot/immediate), ascending toward cool/light (per the user's rule).
const COLORS: Record<number, string> = {
  1: '#e5484d',
  2: '#f76808',
  3: '#ffb224',
  4: '#5b9dd9',
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
