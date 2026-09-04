// The search-click payload, as the wire actually spells it.
//
// The client speaks camelCase and the endpoint speaks snake_case, and
// the failure is silent in both directions: a mis-spelled field is
// dropped by the server without an error, and the recall sensor then
// under-counts a whole surface while its results still count in the
// denominator. Nothing in a type checker sees that, so it is asserted.

import { describe, expect, it } from 'vitest'
import { SEARCH_CLICK_PATH, searchClickBody } from './telemetry'

describe('searchClickBody', () => {
  it('maps every field to the name the endpoint reads', () => {
    expect(
      searchClickBody({
        q: 'preventivo',
        hitKind: 'task',
        hitId: 'abc',
        rank: 3,
        resultCount: 20,
      }),
    ).toEqual({
      q: 'preventivo',
      hit_kind: 'task',
      hit_id: 'abc',
      rank: 3,
      result_count: 20,
    })
  })

  it('sends nothing the caller did not put in the event', () => {
    const body = searchClickBody({ q: 'x', hitKind: 'note', hitId: 'n', rank: 1, resultCount: 1 })
    expect(Object.keys(body).sort()).toEqual(['hit_id', 'hit_kind', 'q', 'rank', 'result_count'])
  })

  it('names the endpoint once', () => {
    expect(SEARCH_CLICK_PATH).toBe('/search/click')
  })
})
