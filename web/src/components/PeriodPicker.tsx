import { useTranslation } from 'react-i18next'
import { periodRange, type Period } from '../lib/period'

const ALL_PERIODS: Period[] = ['day', 'week', 'month', 'custom']

interface Props {
  period: Period
  anchor: Date
  // Single callback so the parent stays the source of truth for both
  // the period kind and the anchor Date it navigates within.
  onChange: (period: Period, anchor: Date) => void
  // Which chips to offer. Defaults to day/week/month/custom; callers
  // that have no free-range UI can drop 'custom'.
  periods?: Period[]
}

// Period selector chips + previous/next arrows (#65). The arrows and
// the month/week/day label are only shown for non-custom periods,
// since "previous/next" is undefined for an arbitrary range.
export function PeriodPicker({ period, anchor, onChange, periods = ALL_PERIODS }: Props) {
  const { t } = useTranslation()
  const info = period === 'custom' ? null : periodRange(period, anchor)
  return (
    <div className="periodbar">
      {periods.map((p) => (
        <button
          key={p}
          type="button"
          className={'btn--sm' + (period === p ? '' : ' btn--ghost')}
          onClick={() => onChange(p, p === 'custom' ? anchor : new Date())}
        >
          {t(`time.period_${p}`)}
        </button>
      ))}
      {info && (
        <span className="periodbar__nav">
          <button
            type="button"
            className="btn--ghost btn--sm"
            onClick={() => onChange(period, info.prevAnchor)}
            title={t('time.periodPrev')}
          >
            ◀
          </button>
          <strong className="periodbar__label">{info.label}</strong>
          <button
            type="button"
            className="btn--ghost btn--sm"
            onClick={() => onChange(period, info.nextAnchor)}
            title={t('time.periodNext')}
          >
            ▶
          </button>
        </span>
      )}
    </div>
  )
}
