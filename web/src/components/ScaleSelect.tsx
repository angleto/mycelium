import { useTranslation } from 'react-i18next'

// A 1..5 select that shows i18n labels (not bare numbers) for the
// Eisenhower importance/urgency axes. labelsKey points at a 5-item
// string array in the catalog (tasks.impLabels / tasks.urgLabels).
// Both axes are mandatory since migration 0102 (Low/Low default
// applied at the backend), so the select is always non-nullable.
export function ScaleSelect({
  value,
  onChange,
  labelsKey,
}: {
  value: number
  onChange: (n: number) => void
  labelsKey: string
}) {
  const { t } = useTranslation()
  const labels = t(labelsKey, { returnObjects: true }) as unknown as string[]
  return (
    <select value={value} onChange={(e) => onChange(Number(e.target.value))}>
      {[1, 2, 3, 4, 5].map((n) => (
        <option key={n} value={n}>
          {labels[n - 1] ?? String(n)}
        </option>
      ))}
    </select>
  )
}
