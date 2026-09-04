// The recents contract, against the two defects it was written for.
//
// One: a flat storage key listed workspace A's titles in workspace B --
// visible cross-tenant leakage, since the rows carry titles. So a caller
// that cannot name a workspace must get no key at all rather than a
// shared one.
//
// Two: recording a row before its entity resolved stored the empty
// loading placeholder, and the row then rendered as a blank line
// forever, because what was stored was the blank.

import { describe, expect, it } from 'vitest'
import {
  RECENTS_MAX,
  type RecentItem,
  isRecentItem,
  parseRecents,
  recentsKey,
  withRecent,
} from './recents'

const row = (n: number): RecentItem => ({
  kind: 'task',
  id: `id-${n}`,
  title: `Task ${n}`,
  route: `/tasks/id-${n}`,
})

describe('recentsKey', () => {
  it('scopes to the workspace', () => {
    expect(recentsKey('ws-1')).toBe('mycelium.recents.v1:ws-1')
  })

  it('is null without one, so no row is ever written to a shared key', () => {
    expect(recentsKey(null)).toBeNull()
    expect(recentsKey(undefined)).toBeNull()
    expect(recentsKey('')).toBeNull()
  })
})

describe('parseRecents', () => {
  it('degrades to empty rather than throwing', () => {
    expect(parseRecents(null)).toEqual([])
    expect(parseRecents('')).toEqual([])
    expect(parseRecents('{not json')).toEqual([])
    expect(parseRecents('{"a":1}')).toEqual([])
  })

  it('drops malformed rows one by one, not the whole list', () => {
    const raw = JSON.stringify([row(1), { kind: 'task' }, row(2), null])
    expect(parseRecents(raw).map((r) => r.id)).toEqual(['id-1', 'id-2'])
  })

  it('caps what it returns even if storage held more', () => {
    const raw = JSON.stringify(Array.from({ length: 40 }, (_, i) => row(i)))
    expect(parseRecents(raw)).toHaveLength(RECENTS_MAX)
  })
})

describe('withRecent', () => {
  it('refuses a row whose title has not resolved yet', () => {
    const before = [row(1)]
    expect(withRecent(before, { ...row(2), title: '' })).toEqual(before)
    expect(withRecent(before, { ...row(2), title: '   ' })).toEqual(before)
  })

  it('moves a revisited route to the front instead of repeating it', () => {
    const list = withRecent([row(1), row(2), row(3)], row(3))
    expect(list.map((r) => r.id)).toEqual(['id-3', 'id-1', 'id-2'])
  })

  it('keeps a re-opened row once, with the newer title', () => {
    const renamed = { ...row(1), title: 'Renamed' }
    const list = withRecent([row(1), row(2)], renamed)
    expect(list.filter((r) => r.route === renamed.route)).toHaveLength(1)
    expect(list[0].title).toBe('Renamed')
  })

  it('caps the list', () => {
    let list: RecentItem[] = []
    for (let i = 0; i < 30; i += 1) list = withRecent(list, row(i))
    expect(list).toHaveLength(RECENTS_MAX)
    expect(list[0].id).toBe('id-29')
  })

  it('does not mutate the list it was given', () => {
    const before = [row(1)]
    withRecent(before, row(2))
    expect(before).toHaveLength(1)
  })
})

describe('isRecentItem', () => {
  it('accepts only the two kinds the palette can route', () => {
    expect(isRecentItem(row(1))).toBe(true)
    expect(isRecentItem({ ...row(1), kind: 'blob' })).toBe(false)
  })
})
