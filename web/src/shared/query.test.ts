// The grammar, as the two surfaces both have to read it.
//
// The tokenizer is shared so that ``a|b`` means a union on /tasks and a
// union in the extension's panel, rather than a union on one and a free
// text search for the literal "a|b" on the other. The key set is closed
// for the same reason: a surface may decline to honour a key, but it may
// never give one a second meaning, and it may not invent an eighth.

import { describe, expect, it } from 'vitest'
import {
  DUE_KEYWORDS,
  FILTER_KEYS,
  RE_DAY_OFFSET,
  RE_PREDICATE,
  RE_YMD,
  isFilterKey,
  tokenize,
} from './query'

describe('tokenize', () => {
  it('splits on whitespace and drops the gaps', () => {
    expect(tokenize('  pane   sourdough ')).toEqual(['pane', 'sourdough'])
  })

  it('keeps | as its own token even typed without spaces', () => {
    expect(tokenize('a|b')).toEqual(['a', '|', 'b'])
    expect(tokenize('a | b')).toEqual(['a', '|', 'b'])
    expect(tokenize('a  |b')).toEqual(['a', '|', 'b'])
  })

  it('is empty for an empty query', () => {
    expect(tokenize('')).toEqual([])
    expect(tokenize('   ')).toEqual([])
  })
})

describe('RE_PREDICATE', () => {
  it('splits a structured atom at the FIRST colon, so values may contain one', () => {
    const m = RE_PREDICATE.exec('created:after:2026-01-01')
    expect(m?.[1]).toBe('created')
    expect(m?.[2]).toBe('after:2026-01-01')
  })

  it('does not match free text or a tag reference', () => {
    expect(RE_PREDICATE.exec('preventivo')).toBeNull()
    expect(RE_PREDICATE.exec('@legale')).toBeNull()
  })

  it('does not match a key with a trailing colon and no value', () => {
    expect(RE_PREDICATE.exec('state:')).toBeNull()
  })
})

describe('the closed key set', () => {
  it('is exactly what both surfaces agreed on', () => {
    expect([...FILTER_KEYS]).toEqual([
      'tag',
      'state',
      'due',
      'priority',
      'created',
      'executor',
      'actor',
    ])
  })

  it('recognises a key case-insensitively at the caller, not here', () => {
    expect(isFilterKey('state')).toBe(true)
    expect(isFilterKey('State')).toBe(false)
  })

  it('refuses a key nobody declared', () => {
    expect(isFilterKey('assignee')).toBe(false)
    expect(isFilterKey('')).toBe(false)
  })
})

describe('the due vocabulary', () => {
  it('offers only shorthands both surfaces can parse', () => {
    expect([...DUE_KEYWORDS]).toEqual(['today', 'tomorrow', 'overdue', 'none'])
  })

  it('reads a signed day offset', () => {
    expect(RE_DAY_OFFSET.exec('+7d')?.slice(1, 3)).toEqual(['+', '7'])
    expect(RE_DAY_OFFSET.exec('-3d')?.slice(1, 3)).toEqual(['-', '3'])
    expect(RE_DAY_OFFSET.exec('7d')).toBeNull()
    expect(RE_DAY_OFFSET.exec('+7')).toBeNull()
  })

  it('reads an absolute calendar day and nothing looser', () => {
    expect(RE_YMD.test('2026-09-03')).toBe(true)
    expect(RE_YMD.test('2026-9-3')).toBe(false)
    expect(RE_YMD.test('2026-09-03T10:00:00Z')).toBe(false)
  })
})
