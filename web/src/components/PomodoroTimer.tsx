import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { formatMmSs, usePomodoro, type Phase } from '../lib/pomodoro'

const PHASE_LABEL_KEY: Record<Phase, string> = {
  idle: 'pomodoro.idle',
  focus: 'pomodoro.focus',
  short_break: 'pomodoro.shortBreak',
  long_break: 'pomodoro.longBreak',
}

const POSITION_KEY = 'flow.pomodoro.collapsed'

function loadCollapsed(): boolean {
  try {
    return localStorage.getItem(POSITION_KEY) === '1'
  } catch {
    return false
  }
}

function saveCollapsed(v: boolean): void {
  try {
    localStorage.setItem(POSITION_KEY, v ? '1' : '0')
  } catch {
    /* ignore */
  }
}

// Floating bottom-right pomodoro dock. Collapsed = a single small chip
// showing the current phase + remaining time (click to expand). Expanded
// = phase, big timer, primary action (start/pause/resume), skip/stop,
// daily counter.
export function PomodoroTimer() {
  const { t } = useTranslation()
  const p = usePomodoro()
  const [collapsed, setCollapsed] = useState<boolean>(loadCollapsed)

  useEffect(() => {
    saveCollapsed(collapsed)
  }, [collapsed])

  // When idle and collapsed, show a slim "open" pill instead of the
  // chip with the timer (the timer is 00:00 which is useless info).
  const isIdle = p.session.phase === 'idle'
  const phaseLabel = t(PHASE_LABEL_KEY[p.session.phase] || 'pomodoro.idle')

  if (collapsed) {
    return (
      <button
        type="button"
        className={
          'pomodoro pomodoro--chip' +
          (p.isRunning ? ' pomodoro--running' : '') +
          (p.isPaused ? ' pomodoro--paused' : '')
        }
        onClick={() => setCollapsed(false)}
        title={t('pomodoro.title')}
      >
        <span className="pomodoro__icon" aria-hidden="true">
          {isIdle ? '🍅' : p.session.phase === 'focus' ? '🎯' : '☕'}
        </span>
        {!isIdle && <span className="pomodoro__mono">{formatMmSs(p.remainingSec)}</span>}
        {isIdle && p.session.completedFocusToday > 0 && (
          <span className="pomodoro__count">{p.session.completedFocusToday}</span>
        )}
      </button>
    )
  }

  return (
    <div className="pomodoro pomodoro--dock">
      <header className="pomodoro__bar">
        <strong>{t('pomodoro.title')}</strong>
        <button
          type="button"
          className="btn--ghost btn--sm"
          aria-label={t('pomodoro.minimize')}
          onClick={() => setCollapsed(true)}
        >
          –
        </button>
      </header>
      <p className="pomodoro__phase">{phaseLabel}</p>
      <p className="pomodoro__time pomodoro__mono">
        {isIdle
          ? formatMmSs(p.config.focusMin * 60)
          : formatMmSs(p.remainingSec)}
      </p>
      <div className="pomodoro__cycle">
        {Array.from({ length: p.config.cyclesBeforeLongBreak }, (_, i) => (
          <span
            key={i}
            className={
              'pomodoro__dot' +
              (i < p.session.completedFocusInCycle ? ' pomodoro__dot--on' : '')
            }
          />
        ))}
      </div>
      <div className="pomodoro__actions">
        {isIdle && (
          <button
            type="button"
            className="btn btn--primary btn--sm"
            onClick={() => p.start('focus')}
          >
            {t('pomodoro.start')}
          </button>
        )}
        {p.isRunning && (
          <button type="button" className="btn--ghost btn--sm" onClick={p.pause}>
            {t('pomodoro.pause')}
          </button>
        )}
        {p.isPaused && (
          <button type="button" className="btn--ghost btn--sm" onClick={p.resume}>
            {t('pomodoro.resume')}
          </button>
        )}
        {!isIdle && (
          <button
            type="button"
            className="btn--ghost btn--sm"
            onClick={p.skip}
            title={t('pomodoro.skipTip')}
          >
            {t('pomodoro.skip')}
          </button>
        )}
        {!isIdle && (
          <button type="button" className="btn--ghost btn--sm" onClick={p.stop}>
            {t('pomodoro.stop')}
          </button>
        )}
        {isIdle && p.session.completedFocusInCycle === 0 && p.session.completedFocusToday > 0 && (
          <button
            type="button"
            className="btn--ghost btn--sm"
            onClick={() => p.start('long_break')}
          >
            {t('pomodoro.startLongBreak')}
          </button>
        )}
        {isIdle && p.session.completedFocusInCycle > 0 && (
          <button
            type="button"
            className="btn--ghost btn--sm"
            onClick={() => p.start('short_break')}
          >
            {t('pomodoro.startShortBreak')}
          </button>
        )}
      </div>
      <footer className="pomodoro__foot">
        <span>{t('pomodoro.todayLabel', { n: p.session.completedFocusToday })}</span>
      </footer>
    </div>
  )
}
