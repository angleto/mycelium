import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { formatMmSs, usePomodoro, type Phase } from '../lib/pomodoro'
import { useMediaQuery } from '../lib/useMediaQuery'

// Below this width the popover would either spill off-screen or pin
// the user against a corner; switch to a centred fixed modal instead.
// Larger than the 640 small-phone breakpoint so phones in landscape
// (~700px wide) also get the modal — anything narrow enough to make
// the anchored popover feel cramped goes through here.
const POMODORO_MODAL_QUERY = '(max-width: 720px)'

const PHASE_LABEL_KEY: Record<Phase, string> = {
  idle: 'pomodoro.idle',
  focus: 'pomodoro.focus',
  short_break: 'pomodoro.shortBreak',
  long_break: 'pomodoro.longBreak',
}

const PHASE_ICON: Record<Phase, string> = {
  idle: '🍅',
  focus: '🎯',
  short_break: '☕',
  long_break: '🌿',
}

// Topbar widget: a single button showing phase, mm:ss and a progress bar.
// Click toggles a popover with start/pause/skip/stop, edit-running controls,
// and inline settings. Lives in topbar__actions (see AppShell), not floating.
export function PomodoroTimer() {
  const { t } = useTranslation()
  const p = usePomodoro()
  const [open, setOpen] = useState(false)
  const wrapRef = useRef<HTMLDivElement | null>(null)
  // Below the phone width the popover renders as a centred modal with
  // a backdrop instead of an anchored dropdown; see POMODORO_MODAL_QUERY.
  const asModal = useMediaQuery(POMODORO_MODAL_QUERY)

  // Close popover on outside click / Escape.
  useEffect(() => {
    if (!open) return
    function onDocMouse(e: MouseEvent): void {
      const el = wrapRef.current
      if (!el) return
      if (e.target instanceof Node && el.contains(e.target)) return
      setOpen(false)
    }
    function onKey(e: KeyboardEvent): void {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onDocMouse)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDocMouse)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  const isIdle = p.session.phase === 'idle'
  const phaseLabel = t(PHASE_LABEL_KEY[p.session.phase] || 'pomodoro.idle')
  const displaySec = isIdle ? p.config.focusMin * 60 : p.remainingSec
  const progressPct = Math.round(p.progress * 100)

  return (
    <div
      className={'pomodoro' + (open && asModal ? ' pomodoro--modal' : '')}
      ref={wrapRef}
    >
      <button
        type="button"
        className={
          'pomodoro__trigger' +
          (p.isRunning ? ' pomodoro__trigger--running' : '') +
          (p.isPaused ? ' pomodoro__trigger--paused' : '') +
          (isIdle ? ' pomodoro__trigger--idle' : '')
        }
        aria-expanded={open}
        aria-label={t('pomodoro.openTimer')}
        title={t('pomodoro.title')}
        onClick={() => setOpen((v) => !v)}
      >
        <span className="pomodoro__icon" aria-hidden="true">
          {PHASE_ICON[p.session.phase]}
        </span>
        <span className="pomodoro__mono">{formatMmSs(displaySec)}</span>
        <span
          className="pomodoro__bar"
          role="progressbar"
          aria-label={t('pomodoro.progress')}
          aria-valuenow={progressPct}
          aria-valuemin={0}
          aria-valuemax={100}
        >
          <span className="pomodoro__bar-fill" style={{ width: `${progressPct}%` }} />
        </span>
        {isIdle && p.session.completedFocusToday > 0 && (
          <span className="pomodoro__count">{p.session.completedFocusToday}</span>
        )}
      </button>
      {open && asModal && (
        <div
          className="pomodoro__backdrop"
          aria-hidden="true"
          onClick={() => setOpen(false)}
        />
      )}
      {open && <PomodoroPopover phaseLabel={phaseLabel} p={p} />}
    </div>
  )
}

function PomodoroPopover({
  phaseLabel,
  p,
}: {
  phaseLabel: string
  p: ReturnType<typeof usePomodoro>
}) {
  const { t } = useTranslation()
  const isIdle = p.session.phase === 'idle'
  const [editVal, setEditVal] = useState<string>(() => formatMmSs(p.remainingSec))
  // Keep the absolute-set input in sync with the live remaining time when
  // not editing it. We track focus to avoid clobbering user typing.
  const editRef = useRef<HTMLInputElement | null>(null)
  useEffect(() => {
    if (editRef.current && document.activeElement === editRef.current) return
    setEditVal(formatMmSs(p.remainingSec))
  }, [p.remainingSec])

  function applyAbsolute(): void {
    const m = /^\s*(\d{1,3}):([0-5]?\d)\s*$/.exec(editVal)
    if (!m) return
    const mins = Number(m[1])
    const secs = Number(m[2])
    if (!Number.isFinite(mins) || !Number.isFinite(secs)) return
    p.setRemaining(mins * 60_000 + secs * 1_000)
  }

  return (
    <div className="pomodoro__pop" role="dialog" aria-label={t('pomodoro.title')}>
      <header className="pomodoro__pop-head">
        <strong>{t('pomodoro.title')}</strong>
        <span className="pomodoro__phase">{phaseLabel}</span>
      </header>
      <p className="pomodoro__time pomodoro__mono">
        {isIdle ? formatMmSs(p.config.focusMin * 60) : formatMmSs(p.remainingSec)}
      </p>
      <div className="pomodoro__cycle" aria-label={t('pomodoro.cycleLabel')}>
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
      {!isIdle && (
        <fieldset className="pomodoro__edit">
          <legend>{t('pomodoro.editRunning')}</legend>
          <div className="pomodoro__edit-row">
            <button
              type="button"
              className="btn--ghost btn--sm"
              onClick={() => p.adjustEndsAt(-5 * 60_000)}
            >
              −5m
            </button>
            <button
              type="button"
              className="btn--ghost btn--sm"
              onClick={() => p.adjustEndsAt(-60_000)}
            >
              −1m
            </button>
            <button
              type="button"
              className="btn--ghost btn--sm"
              onClick={() => p.adjustEndsAt(60_000)}
            >
              +1m
            </button>
            <button
              type="button"
              className="btn--ghost btn--sm"
              onClick={() => p.adjustEndsAt(5 * 60_000)}
            >
              +5m
            </button>
          </div>
          <div className="pomodoro__edit-row">
            <label className="pomodoro__edit-set">
              {t('pomodoro.setRemaining')}
              <input
                ref={editRef}
                type="text"
                inputMode="numeric"
                pattern="\d{1,3}:[0-5]?\d"
                value={editVal}
                onChange={(e) => setEditVal(e.target.value)}
                onBlur={applyAbsolute}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') {
                    e.preventDefault()
                    applyAbsolute()
                  }
                }}
                placeholder="mm:ss"
                style={{ width: '5rem' }}
              />
            </label>
          </div>
        </fieldset>
      )}
      <fieldset className="pomodoro__settings">
        <legend>{t('pomodoro.settingsTitle')}</legend>
        <div className="pomodoro__settings-grid">
          <label>
            <span>{t('pomodoro.focusMin')}</span>
            <input
              type="number"
              min={1}
              max={180}
              value={p.config.focusMin}
              onChange={(e) => {
                const n = Number(e.target.value)
                if (Number.isFinite(n) && n >= 1 && n <= 180) {
                  p.updateConfig({ focusMin: n })
                }
              }}
            />
          </label>
          <label>
            <span>{t('pomodoro.shortBreakMin')}</span>
            <input
              type="number"
              min={1}
              max={60}
              value={p.config.shortBreakMin}
              onChange={(e) => {
                const n = Number(e.target.value)
                if (Number.isFinite(n) && n >= 1 && n <= 60) {
                  p.updateConfig({ shortBreakMin: n })
                }
              }}
            />
          </label>
          <label>
            <span>{t('pomodoro.longBreakMin')}</span>
            <input
              type="number"
              min={1}
              max={120}
              value={p.config.longBreakMin}
              onChange={(e) => {
                const n = Number(e.target.value)
                if (Number.isFinite(n) && n >= 1 && n <= 120) {
                  p.updateConfig({ longBreakMin: n })
                }
              }}
            />
          </label>
          <label>
            <span>{t('pomodoro.cyclesBeforeLongBreak')}</span>
            <input
              type="number"
              min={2}
              max={10}
              value={p.config.cyclesBeforeLongBreak}
              onChange={(e) => {
                const n = Number(e.target.value)
                if (Number.isFinite(n) && n >= 2 && n <= 10) {
                  p.updateConfig({ cyclesBeforeLongBreak: n })
                }
              }}
            />
          </label>
        </div>
        <div className="pomodoro__settings-row">
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
      </fieldset>
      <footer className="pomodoro__foot">
        <span>{t('pomodoro.todayLabel', { n: p.session.completedFocusToday })}</span>
      </footer>
    </div>
  )
}
