import { useTranslation } from 'react-i18next'

// A 1..5 select that shows i18n labels (not bare numbers) for the
// Eisenhower importance/urgency axes. labelsKey points at a 5-item
// string array in the catalog (tasks.impLabels / tasks.urgLabels).
// nullable=true surfaces a "non impostato" placeholder so a task with
// a missing axis (typical for MCP-created tasks) is shown as missing
// rather than silently rendered as "Bassa" (4). onChange always emits
// a real number — clearing back to null isn't a user-driven action.
export function ScaleSelect({
  value,
  onChange,
  labelsKey,
  nullable = false,
}: {
  value: number | null
  onChange: (n: number) => void
  labelsKey: string
  nullable?: boolean
}) {
  const { t } = useTranslation()
  const labels = t(labelsKey, { returnObjects: true }) as unknown as string[]
  return (
    <select
      value={value ?? ''}
      onChange={(e) => onChange(Number(e.target.value))}
    >
      {nullable && (
        <option value="" disabled>
          {t('tasks.notSet')}
        </option>
      )}
      {[1, 2, 3, 4, 5].map((n) => (
        <option key={n} value={n}>
          {labels[n - 1] ?? String(n)}
        </option>
      ))}
    </select>
  )
}
