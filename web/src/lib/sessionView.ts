import type { TFunction } from 'i18next'
import type { components } from '../shared'

// How one session reads to a person: what it is, what it is doing in words,
// and the quieter facts after. Pure, so the wording is tested without a
// page around it, and shared by the page and the tasks board.
//
// The server decides the GROUP and the ORDER (``services/agent_sessions``),
// so the CLI and the web cannot disagree about who needs the person first.
// What is decided here is only how that reads.
export type SessionRow = components['schemas']['SessionRowOut']

/** A duration as a person says it: "40 s", "12 min", "3 h 5 min", "2 d". */
export function waited(seconds: number, t: TFunction): string {
  const s = Math.max(0, Math.floor(seconds))
  if (s < 60) return t('sessions.dur.s', { n: s })
  const min = Math.floor(s / 60)
  if (min < 60) return t('sessions.dur.min', { n: min })
  const h = Math.floor(min / 60)
  if (h < 24) {
    const rest = min % 60
    return rest === 0 ? t('sessions.dur.h', { n: h }) : t('sessions.dur.hmin', { h, min: rest })
  }
  return t('sessions.dur.d', { n: Math.floor(h / 24) })
}

function since(iso: string | null | undefined, nowMs: number): number {
  if (!iso) return 0
  return (nowMs - new Date(iso).getTime()) / 1000
}

/** What the row is called. Never an id: a session is named by the project
 * it works on, then by what its host or its worker calls it, then by the
 * machine, and only then by what it is not ("a session"). */
export function sessionName(r: SessionRow, t: TFunction): string {
  return r.project || r.title || r.label || r.machine || t('sessions.unnamed')
}

/** The state, in words, with how long it has lasted. */
export function sessionState(r: SessionRow, nowMs: number, t: TFunction): string {
  if (r.agent === 'unknown' || r.attention == null) {
    return t('sessions.state.notReporting')
  }
  if (r.group === 'quiet') {
    return t('sessions.state.quiet', { d: waited(since(r.heard_at, nowMs), t) })
  }
  const d = waited(since(r.attention_since, nowMs), t)
  switch (r.attention) {
    case 'blocked':
      return r.blocked_on === 'question'
        ? t('sessions.state.question', { d })
        : t('sessions.state.permission', { d })
    case 'your_turn':
      return t('sessions.state.yourTurn', { d })
    case 'working':
      return t('sessions.state.working', { d })
  }
}

/** The tone of the state: what a glance should pick out. A blocked session
 * cannot go on at all; a finished one only waits for more. */
export function sessionTone(r: SessionRow): 'blocked' | 'turn' | 'working' | 'quiet' {
  if (r.group === 'quiet' || r.agent === 'unknown') return 'quiet'
  if (r.attention === 'blocked') return 'blocked'
  if (r.attention === 'your_turn') return 'turn'
  return 'working'
}

/** The quieter facts: where it runs, what else it is called, when it was
 * last heard from. Only what adds to the name: the project already leads. */
export function sessionMeta(r: SessionRow, nowMs: number, t: TFunction): string[] {
  const name = sessionName(r, t)
  const parts: string[] = []
  if (r.machine && r.machine !== name) parts.push(r.machine)
  const alias = r.title || r.label
  if (alias && alias !== name) parts.push(alias)
  // A quiet row's state already says how long it has been silent.
  if (r.group !== 'quiet') {
    parts.push(t('sessions.meta.heard', { d: waited(since(r.heard_at, nowMs), t) }))
  }
  return parts
}

export type SessionMessage = components['schemas']['SessionMessageOut']

/** What became of a message the person sent, in words (docs/adr/0079).
 * "Delivered" says only that it was written into the session: the session
 * answers nothing either way, and its row moving to working is the sign
 * that it was read. */
export function messageState(m: SessionMessage, t: TFunction, locale: string): string {
  switch (m.status) {
    case 'queued':
      return t('sessions.message.queued')
    case 'claimed':
      return t('sessions.message.claimed')
    case 'uncertain':
      return t('sessions.message.uncertain')
    case 'delivered':
      return t('sessions.message.delivered', {
        when: new Date(m.status_since).toLocaleTimeString(locale, {
          hour: '2-digit',
          minute: '2-digit',
        }),
      })
    case 'failed':
      return m.failure
        ? t('sessions.message.failed', { why: m.failure })
        : t('sessions.message.failedNoReason')
    case 'not_delivered':
      return t('sessions.message.notDelivered')
  }
}
