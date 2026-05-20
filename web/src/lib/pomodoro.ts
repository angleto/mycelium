import { useCallback, useEffect, useReducer, useRef } from 'react'

// Pomodoro state machine (purely client-side, single user / single tab
// authoritative). Persisted to localStorage so a reload keeps the
// running session intact. The TICKER is virtual: we store the absolute
// `endsAt` epoch for the current phase and recompute remaining seconds
// each render frame; this is robust against tab-throttle / clock skew
// (no drift across long focus blocks while the tab is in background).

export type Phase = 'idle' | 'focus' | 'short_break' | 'long_break'

export type PomodoroConfig = {
  focusMin: number
  shortBreakMin: number
  longBreakMin: number
  cyclesBeforeLongBreak: number
  notify: boolean
  sound: boolean
}

const DEFAULT_CONFIG: PomodoroConfig = {
  focusMin: 25,
  shortBreakMin: 5,
  longBreakMin: 15,
  cyclesBeforeLongBreak: 4,
  notify: true,
  sound: true,
}

export type PomodoroSession = {
  phase: Phase
  endsAt: number // epoch ms; meaningful only when phase !== 'idle'
  pausedRemainingMs: number | null // when paused, holds remaining ms
  completedFocusToday: number
  completedFocusInCycle: number
  startedDay: string // YYYY-MM-DD; rolls completedFocusToday over midnight
}

function todayStr(): string {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(
    d.getDate(),
  ).padStart(2, '0')}`
}

const INITIAL_SESSION: PomodoroSession = {
  phase: 'idle',
  endsAt: 0,
  pausedRemainingMs: null,
  completedFocusToday: 0,
  completedFocusInCycle: 0,
  startedDay: todayStr(),
}

const CFG_KEY = 'flow.pomodoro.config'
const SESS_KEY = 'flow.pomodoro.session'

function loadConfig(): PomodoroConfig {
  try {
    const raw = localStorage.getItem(CFG_KEY)
    if (!raw) return DEFAULT_CONFIG
    const parsed = JSON.parse(raw) as Partial<PomodoroConfig>
    return { ...DEFAULT_CONFIG, ...parsed }
  } catch {
    return DEFAULT_CONFIG
  }
}

function loadSession(): PomodoroSession {
  try {
    const raw = localStorage.getItem(SESS_KEY)
    if (!raw) return INITIAL_SESSION
    const parsed = JSON.parse(raw) as PomodoroSession
    // Roll daily counter if the persisted day differs from today.
    if (parsed.startedDay !== todayStr()) {
      return { ...parsed, completedFocusToday: 0, startedDay: todayStr() }
    }
    return parsed
  } catch {
    return INITIAL_SESSION
  }
}

function saveConfig(cfg: PomodoroConfig): void {
  try {
    localStorage.setItem(CFG_KEY, JSON.stringify(cfg))
  } catch {
    /* quota / private mode: degrade silently */
  }
}

function saveSession(s: PomodoroSession): void {
  try {
    localStorage.setItem(SESS_KEY, JSON.stringify(s))
  } catch {
    /* ignore */
  }
}

function phaseDurationMs(phase: Phase, cfg: PomodoroConfig): number {
  switch (phase) {
    case 'focus':
      return cfg.focusMin * 60_000
    case 'short_break':
      return cfg.shortBreakMin * 60_000
    case 'long_break':
      return cfg.longBreakMin * 60_000
    case 'idle':
      return 0
  }
}

function nextPhase(current: Phase, completedFocusInCycle: number, cfg: PomodoroConfig): Phase {
  if (current === 'focus') {
    return completedFocusInCycle % cfg.cyclesBeforeLongBreak === 0
      ? 'long_break'
      : 'short_break'
  }
  // After any break the user starts a focus block again (manual click).
  return 'focus'
}

type State = { config: PomodoroConfig; session: PomodoroSession }

type Action =
  | { type: 'start'; phase: Phase }
  | { type: 'pause' }
  | { type: 'resume' }
  | { type: 'skip' }
  | { type: 'stop' }
  | { type: 'tickComplete' }
  | { type: 'updateConfig'; patch: Partial<PomodoroConfig> }
  | { type: 'rollDay' }

function reducer(state: State, action: Action): State {
  const { config, session } = state
  switch (action.type) {
    case 'start': {
      const dur = phaseDurationMs(action.phase, config)
      const sess: PomodoroSession = {
        ...session,
        phase: action.phase,
        endsAt: Date.now() + dur,
        pausedRemainingMs: null,
      }
      saveSession(sess)
      return { config, session: sess }
    }
    case 'pause': {
      if (session.phase === 'idle' || session.pausedRemainingMs !== null) return state
      const remaining = Math.max(0, session.endsAt - Date.now())
      const sess: PomodoroSession = { ...session, pausedRemainingMs: remaining }
      saveSession(sess)
      return { config, session: sess }
    }
    case 'resume': {
      if (session.phase === 'idle' || session.pausedRemainingMs === null) return state
      const sess: PomodoroSession = {
        ...session,
        endsAt: Date.now() + session.pausedRemainingMs,
        pausedRemainingMs: null,
      }
      saveSession(sess)
      return { config, session: sess }
    }
    case 'skip':
    case 'tickComplete': {
      if (session.phase === 'idle') return state
      const wasFocus = session.phase === 'focus'
      const completedFocusInCycle = wasFocus
        ? (session.completedFocusInCycle + 1) % config.cyclesBeforeLongBreak
        : session.completedFocusInCycle
      const completedFocusToday = wasFocus
        ? session.completedFocusToday + 1
        : session.completedFocusToday
      const sess: PomodoroSession = {
        ...session,
        phase: 'idle', // user clicks Start to enter the next phase explicitly
        endsAt: 0,
        pausedRemainingMs: null,
        completedFocusInCycle,
        completedFocusToday,
      }
      saveSession(sess)
      return { config, session: sess }
    }
    case 'stop': {
      const sess: PomodoroSession = {
        ...session,
        phase: 'idle',
        endsAt: 0,
        pausedRemainingMs: null,
      }
      saveSession(sess)
      return { config, session: sess }
    }
    case 'updateConfig': {
      const cfg = { ...config, ...action.patch }
      saveConfig(cfg)
      return { config: cfg, session }
    }
    case 'rollDay': {
      if (session.startedDay === todayStr()) return state
      const sess: PomodoroSession = {
        ...session,
        completedFocusToday: 0,
        startedDay: todayStr(),
      }
      saveSession(sess)
      return { config, session: sess }
    }
  }
}

function initialState(): State {
  return { config: loadConfig(), session: loadSession() }
}

export type Pomodoro = {
  config: PomodoroConfig
  session: PomodoroSession
  remainingSec: number
  isPaused: boolean
  isRunning: boolean
  suggestedNext: Phase
  start: (phase: Phase) => void
  pause: () => void
  resume: () => void
  skip: () => void
  stop: () => void
  updateConfig: (patch: Partial<PomodoroConfig>) => void
}

function maybeNotify(prev: Phase, suggestedNext: Phase, cfg: PomodoroConfig): void {
  if (!cfg.notify) return
  if (typeof Notification === 'undefined') return
  if (Notification.permission !== 'granted') return
  const which: Record<Phase, string> = {
    focus: 'Focus block done',
    short_break: 'Short break done',
    long_break: 'Long break done',
    idle: '',
  }
  const upNext: Record<Phase, string> = {
    focus: 'Time to focus',
    short_break: 'Take a short break',
    long_break: 'Take a long break',
    idle: '',
  }
  const title = which[prev] || 'Pomodoro'
  const body = upNext[suggestedNext]
  try {
    new Notification(title, { body, tag: 'flow-pomodoro' })
  } catch {
    /* permission revoked between query and call */
  }
}

function beep(cfg: PomodoroConfig): void {
  if (!cfg.sound) return
  try {
    const AC =
      window.AudioContext ||
      (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext
    const ctx = new AC()
    const osc = ctx.createOscillator()
    const gain = ctx.createGain()
    osc.connect(gain)
    gain.connect(ctx.destination)
    osc.type = 'sine'
    osc.frequency.value = 660
    gain.gain.setValueAtTime(0.0001, ctx.currentTime)
    gain.gain.exponentialRampToValueAtTime(0.25, ctx.currentTime + 0.02)
    gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.6)
    osc.start()
    osc.stop(ctx.currentTime + 0.65)
  } catch {
    /* WebAudio blocked: degrade silently */
  }
}

export function usePomodoro(): Pomodoro {
  const [state, dispatch] = useReducer(reducer, undefined, initialState)
  const { config, session } = state
  const tickRef = useRef<number>(0)
  // Force re-render every second while running, without putting a Date
  // into reducer state (cheaper and the source of truth stays endsAt).
  const [, forceTick] = useReducer((n: number) => n + 1, 0)
  // Phase-completion edge detection: when remaining hits zero we fire
  // tickComplete exactly once + dispatch a notification.
  const lastPhaseRef = useRef<Phase>(session.phase)

  useEffect(() => {
    if (session.phase === 'idle' || session.pausedRemainingMs !== null) {
      return
    }
    const id = window.setInterval(() => {
      forceTick()
      const remaining = Math.max(0, session.endsAt - Date.now())
      if (remaining <= 0) {
        window.clearInterval(id)
        const completedFocusInCycle =
          session.phase === 'focus'
            ? (session.completedFocusInCycle + 1) % config.cyclesBeforeLongBreak
            : session.completedFocusInCycle
        const suggested = nextPhase(session.phase, completedFocusInCycle, config)
        const prevPhase = session.phase
        dispatch({ type: 'tickComplete' })
        maybeNotify(prevPhase, suggested, config)
        beep(config)
      }
    }, 250)
    tickRef.current = id
    return () => window.clearInterval(id)
  }, [session.phase, session.endsAt, session.pausedRemainingMs, session.completedFocusInCycle, config])

  useEffect(() => {
    // Day rollover (a session left running past midnight): the next
    // render after midnight resets the daily counter.
    if (session.startedDay !== todayStr()) {
      dispatch({ type: 'rollDay' })
    }
  })

  useEffect(() => {
    lastPhaseRef.current = session.phase
  }, [session.phase])

  // Cross-tab sync: a session change in another tab updates here.
  useEffect(() => {
    function onStorage(e: StorageEvent): void {
      if (e.key === SESS_KEY || e.key === CFG_KEY) forceTick()
    }
    window.addEventListener('storage', onStorage)
    return () => window.removeEventListener('storage', onStorage)
  }, [])

  // Derived from wall-clock; intentionally re-reads Date.now() each render
  // so the visible countdown stays accurate even when interval timers are
  // throttled (background tabs). The setInterval above triggers re-renders.
  // eslint-disable-next-line react-hooks/purity
  const wallNow = Date.now()
  const remainingMs =
    session.pausedRemainingMs !== null
      ? session.pausedRemainingMs
      : session.phase === 'idle'
        ? 0
        : Math.max(0, session.endsAt - wallNow)
  const remainingSec = Math.ceil(remainingMs / 1000)
  const isPaused = session.pausedRemainingMs !== null
  const isRunning = session.phase !== 'idle' && !isPaused

  // What phase Start should jump to. After completing a focus block we
  // suggest the appropriate break; after a break we suggest focus.
  const suggestedNext: Phase =
    session.phase !== 'idle'
      ? session.phase
      : session.completedFocusInCycle === 0 && session.completedFocusToday > 0
      ? 'focus' // last break was a long break: back to focus
      : 'focus' // default first phase

  const start = useCallback(
    (phase: Phase) => {
      if (config.notify && typeof Notification !== 'undefined') {
        if (Notification.permission === 'default') {
          void Notification.requestPermission()
        }
      }
      dispatch({ type: 'start', phase })
    },
    [config.notify],
  )
  const pause = useCallback(() => dispatch({ type: 'pause' }), [])
  const resume = useCallback(() => dispatch({ type: 'resume' }), [])
  const skip = useCallback(() => dispatch({ type: 'skip' }), [])
  const stop = useCallback(() => dispatch({ type: 'stop' }), [])
  const updateConfig = useCallback(
    (patch: Partial<PomodoroConfig>) => dispatch({ type: 'updateConfig', patch }),
    [],
  )

  return {
    config,
    session,
    remainingSec,
    isPaused,
    isRunning,
    suggestedNext,
    start,
    pause,
    resume,
    skip,
    stop,
    updateConfig,
  }
}

export function formatMmSs(sec: number): string {
  const m = Math.floor(sec / 60)
  const s = sec % 60
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
}
