// The code-resolution contract.
//
// Two properties matter and neither is obvious from reading the
// functions. The cache key must separate the two QUESTIONS the endpoint
// answers -- "what entity is this id?" (which sees the archive shelf)
// and "what may I link to?" (which does not) -- or the narrower question
// serves the wider one's cached answer and a chip stops resolving to an
// archived task. And the key must NOT separate two spellings of the same
// question, or the same lookup is paid for twice.

import { describe, expect, it } from 'vitest'
import { RESOLVE_ID, isFullUuid, isPrefixCandidate, lookupCacheKey, lookupPath } from './prefix'

describe('isPrefixCandidate', () => {
  it('accepts the 8-hex code the product writes in backticks', () => {
    expect(isPrefixCandidate('91cf6aaa')).toBe(true)
    expect(isPrefixCandidate('  91CF6AAA  ')).toBe(true)
  })

  it('accepts a hyphenated run up to a full uuid', () => {
    expect(isPrefixCandidate('91cf6aaa-1af3')).toBe(true)
    expect(isPrefixCandidate('16e174d0-1af3-4443-aa22-ed7ab976fada')).toBe(true)
  })

  it('rejects a half-typed uuid ending in a hyphen, so no lookup fires', () => {
    expect(isPrefixCandidate('91cf6aaa-')).toBe(false)
  })

  it('rejects anything that is not code-shaped', () => {
    for (const s of ['', 'ab', 'preventivo', 'g1cf6aaa', '91cf 6aaa', '#91cf6aaa']) {
      expect(isPrefixCandidate(s)).toBe(false)
    }
  })
})

describe('isFullUuid', () => {
  it('distinguishes a complete id from a prefix', () => {
    expect(isFullUuid('16e174d0-1af3-4443-aa22-ed7ab976fada')).toBe(true)
    expect(isFullUuid('16e174d0')).toBe(false)
  })
})

describe('lookupCacheKey', () => {
  it('is stable across kind orderings, so one answer is fetched once', () => {
    expect(lookupCacheKey('abc12345', { kinds: ['note', 'task'] })).toBe(
      lookupCacheKey('abc12345', { kinds: ['task', 'note'] }),
    )
  })

  it('is stable across case and surrounding space', () => {
    expect(lookupCacheKey(' ABC12345 ')).toBe(lookupCacheKey('abc12345'))
  })

  it('separates the two questions the endpoint answers', () => {
    expect(lookupCacheKey('abc12345', RESOLVE_ID)).not.toBe(lookupCacheKey('abc12345', {}))
  })

  it('separates a narrowed kind list from the default', () => {
    expect(lookupCacheKey('abc12345', { kinds: ['task'] })).not.toBe(lookupCacheKey('abc12345'))
  })
})

describe('lookupPath', () => {
  it('asks the plain question with no parameters', () => {
    expect(lookupPath('ABC12345')).toBe('/lookup/abc12345')
  })

  it('spells the perimeter and the kinds the way the endpoint reads them', () => {
    expect(lookupPath('abc12345', { ...RESOLVE_ID, kinds: ['task', 'note'] })).toBe(
      '/lookup/abc12345?kinds=task%2Cnote&include_archived=true',
    )
  })

  it('encodes the prefix: it is user input reaching a URL', () => {
    expect(lookupPath('a/../b')).toBe('/lookup/a%2F..%2Fb')
  })
})
