import { useTranslation } from 'react-i18next'
import { usePomodoro } from '../lib/pomodoro'

export function PomodoroSettings() {
  const { t } = useTranslation()
  const p = usePomodoro()

  function intField(value: number, set: (n: number) => void, min: number, max: number) {
    return (
      <input
        type="number"
        min={min}
        max={max}
        value={value}
        style={{ width: '5rem' }}
        onChange={(e) => {
          const n = Number(e.target.value)
          if (Number.isFinite(n) && n >= min && n <= max) set(n)
        }}
      />
    )
  }

  return (
    <section className="card">
      <h2>{t('pomodoro.settingsTitle')}</h2>
      <p className="hint">{t('pomodoro.settingsHint')}</p>
      <div className="row">
        <label>
          {t('pomodoro.focusMin')}
          {intField(p.config.focusMin, (n) => p.updateConfig({ focusMin: n }), 1, 180)}
        </label>
        <label>
          {t('pomodoro.shortBreakMin')}
          {intField(p.config.shortBreakMin, (n) => p.updateConfig({ shortBreakMin: n }), 1, 60)}
        </label>
        <label>
          {t('pomodoro.longBreakMin')}
          {intField(p.config.longBreakMin, (n) => p.updateConfig({ longBreakMin: n }), 1, 120)}
        </label>
        <label>
          {t('pomodoro.cyclesBeforeLongBreak')}
          {intField(
            p.config.cyclesBeforeLongBreak,
            (n) => p.updateConfig({ cyclesBeforeLongBreak: n }),
            2,
            10,
          )}
        </label>
      </div>
      <div className="row">
        <button
          type="button"
          role="switch"
          aria-checked={p.config.notify}
          className={
            'toggle-pill' + (p.config.notify ? ' toggle-pill--on' : '')
          }
          onClick={() => p.updateConfig({ notify: !p.config.notify })}
        >
          {t('pomodoro.notify')}: {p.config.notify ? t('common.on') : t('common.off')}
        </button>
        <button
          type="button"
          role="switch"
          aria-checked={p.config.sound}
          className={
            'toggle-pill' + (p.config.sound ? ' toggle-pill--on' : '')
          }
          onClick={() => p.updateConfig({ sound: !p.config.sound })}
        >
          {t('pomodoro.sound')}: {p.config.sound ? t('common.on') : t('common.off')}
        </button>
      </div>
    </section>
  )
}
