import { describe, expect, it } from 'vitest'
import i18n from '../i18n'
import {
  messageState,
  sessionMeta,
  sessionName,
  sessionState,
  sessionTone,
  waited,
  type SessionMessage,
  type SessionRow,
} from './sessionView'

// How a session reads to a person. Each case is one that the page it
// replaced got wrong or could not say: a row named by a uuid, no way to tell
// a session waiting for a permission from one that finished, "working" with
// no idea for how long.

const t = i18n.getFixedT('en')
const NOW = Date.parse('2026-11-26T12:00:00Z')
const ago = (sec: number) => new Date(NOW - sec * 1000).toISOString()

function row(over: Partial<SessionRow> = {}): SessionRow {
  return {
    session_id: '00000000-0000-4000-8000-0000000000aa',
    worker_ids: [],
    agent: 'claude_code',
    project: 'humus',
    machine: 'lab',
    title: null,
    label: null,
    attention: 'working',
    blocked_on: null,
    attention_since: ago(40 * 60),
    opened_at: ago(3600),
    heard_at: ago(5),
    group: 'working',
    held: [],
    can_send: false,
    ...over,
  }
}

describe('waited', () => {
  it.each([
    [0, '0 s'],
    [59, '59 s'],
    [60, '1 min'],
    [3599, '59 min'],
    [3600, '1 h'],
    [3600 + 5 * 60, '1 h 5 min'],
    [26 * 3600, '1 d'],
  ])('%i seconds read as %s', (sec, want) => {
    expect(waited(sec, t)).toBe(want)
  })

  it('never reads a negative duration from a skewed clock', () => {
    expect(waited(-30, t)).toBe('0 s')
  })
})

describe('sessionState', () => {
  it('tells a permission from a question, with how long it has waited', () => {
    expect(
      sessionState(row({ attention: 'blocked', blocked_on: 'permission', attention_since: ago(12 * 60), group: 'needs_you' }), NOW, t),
    ).toBe('Waiting for a permission · 12 min')
    expect(
      sessionState(row({ attention: 'blocked', blocked_on: 'question', attention_since: ago(90), group: 'needs_you' }), NOW, t),
    ).toBe('Waiting for an answer · 1 min')
  })

  it('says a finished session waits for the next instruction', () => {
    expect(sessionState(row({ attention: 'your_turn', attention_since: ago(3 * 3600), group: 'needs_you' }), NOW, t)).toBe(
      'Finished, waiting for your next instruction · 3 h',
    )
  })

  it('says how long a session has been working, not since it was last heard', () => {
    expect(sessionState(row(), NOW, t)).toBe('Working · 40 min')
  })

  it('says a quiet session has given no news, measured from when it was heard', () => {
    expect(sessionState(row({ group: 'quiet', heard_at: ago(25 * 60) }), NOW, t)).toBe('No news for 25 min')
  })

  it('says plainly that a session of another client does not report', () => {
    expect(sessionState(row({ agent: 'unknown', attention: null, attention_since: null }), NOW, t)).toBe(
      'Not reporting (another client)',
    )
  })
})

describe('sessionName and sessionMeta', () => {
  it('leads with the project and never shows an id', () => {
    const r = row({ project: null, title: null, label: null, machine: null })
    expect(sessionName(r, t)).toBe('A session')
    expect(sessionName(row({ project: null, label: 'cc-humus' }), t)).toBe('cc-humus')
  })

  it('repeats nothing the name already says', () => {
    const r = row({ project: null, machine: 'lab', title: null, label: null, heard_at: ago(120) })
    expect(sessionName(r, t)).toBe('lab')
    expect(sessionMeta(r, NOW, t)).toEqual(['heard 2 min ago'])
    expect(sessionMeta(row({ title: 'fix-sweep', heard_at: ago(120) }), NOW, t)).toEqual([
      'lab',
      'fix-sweep',
      'heard 2 min ago',
    ])
  })
})

describe('sessionTone', () => {
  it('marks blocked apart from a finished turn', () => {
    expect(sessionTone(row({ attention: 'blocked', blocked_on: 'permission', group: 'needs_you' }))).toBe('blocked')
    expect(sessionTone(row({ attention: 'your_turn', group: 'needs_you' }))).toBe('turn')
    expect(sessionTone(row())).toBe('working')
    expect(sessionTone(row({ group: 'quiet' }))).toBe('quiet')
  })
})

describe('messageState', () => {
  const msg = (over: Partial<SessionMessage>): SessionMessage => ({
    id: '00000000-0000-4000-8000-0000000000m1',
    session_id: '00000000-0000-4000-8000-0000000000aa',
    body: 'rebase onto 2.4',
    status: 'queued',
    status_since: '2026-11-26T12:41:00Z',
    created_at: '2026-11-26T12:40:00Z',
    failure: null,
    ...over,
  })

  it('says each state in words, and never "delivered" for what may not have arrived', () => {
    const say = (over: Partial<SessionMessage>) => messageState(msg(over), t, 'en-GB')
    expect(say({ status: 'queued' })).toBe('Waiting to be delivered')
    expect(say({ status: 'claimed' })).toBe('Being delivered')
    expect(say({ status: 'uncertain' })).toBe('May not have arrived: check the terminal')
    expect(say({ status: 'delivered' })).toMatch(/^Delivered \d\d:41$/)
    expect(say({ status: 'not_delivered' })).toBe('Not delivered: the session ended first')
    expect(say({ status: 'failed', failure: 'connection refused' })).toBe(
      'Not delivered: connection refused',
    )
    expect(say({ status: 'failed' })).toBe('Not delivered')
  })
})
