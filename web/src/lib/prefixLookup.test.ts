import { describe, expect, it } from 'vitest'
import {
  _clearCacheForTest,
  _seedCacheForTest,
  getCachedLookup,
  RESOLVE_ID,
  type LookupOut,
} from './prefixLookup'

// The shared cache answers two different questions through one endpoint
// (task d12f6217): "what entity is this id?" resolves the archive shelf
// too, "what may I offer in a list?" does not. Both go through the same
// module-level Map, so the perimeter has to be part of the key -- without
// it whichever caller asked FIRST decides what every later one sees, and a
// picker query would hide an archived note from the chip that must render
// it (or the chip would feed an archived note into the picker).

const PREFIX = '91cf6aaa'

function answer(matches: LookupOut['matches']): LookupOut {
  return { prefix: PREFIX, matches }
}

const ARCHIVED_NOTE = {
  kind: 'note' as const,
  id: '91cf6aaa-0000-4000-8000-000000000001',
  title: 'Shelved',
  state_name: null,
  is_terminal: null,
  is_archived: true,
  is_deleted: false,
  route_url: '/notes/91cf6aaa-0000-4000-8000-000000000001',
}

describe('prefixLookup cache key', () => {
  it('does not serve an identity resolution out of the picker answer', () => {
    _clearCacheForTest()
    // The picker asked first and got nothing: archived notes are hidden
    // from it by the endpoint's default.
    _seedCacheForTest(PREFIX, answer([]))
    expect(getCachedLookup(PREFIX)?.matches).toEqual([])
    expect(getCachedLookup(PREFIX, RESOLVE_ID)).toBeUndefined()
  })

  it('does not serve the picker out of an identity resolution', () => {
    _clearCacheForTest()
    _seedCacheForTest(PREFIX, answer([ARCHIVED_NOTE]), RESOLVE_ID)
    expect(getCachedLookup(PREFIX, RESOLVE_ID)?.matches).toHaveLength(1)
    expect(getCachedLookup(PREFIX)).toBeUndefined()
  })

  it('still separates the kinds whitelist', () => {
    _clearCacheForTest()
    _seedCacheForTest(PREFIX, answer([ARCHIVED_NOTE]), { ...RESOLVE_ID, kinds: ['note'] })
    expect(getCachedLookup(PREFIX, { ...RESOLVE_ID, kinds: ['note'] })?.matches).toHaveLength(1)
    expect(getCachedLookup(PREFIX, RESOLVE_ID)).toBeUndefined()
  })
})
