// Search-click telemetry, as the payload both clients send.
//
// ADR-0035: which query led a person to open which entity, at which
// 1-based rank of the ranked /search list, out of how many ranked hits.
// The nightly garden-health snapshot aggregates these into the recall
// sensor, so a surface that opens ranked results and does NOT emit this
// does not merely lose a metric: it silently drags the recall figure
// down for every surface, because the opens it serves are counted
// nowhere while its results are counted in the denominator.
//
// Rank is 1-based and is the rank in the RANKED list only. A recents
// row, a code-prefix resolution or a locally filtered row has no rank
// and must not be reported with a fabricated one, which is why the
// caller passes it rather than the list index.
//
// Pure by contract: this directory is compiled into more than one
// package and must import nothing. The transport is the caller's, and
// it is fire-and-forget on both: telemetry must never delay, let alone
// break, a navigation.

export interface SearchClickEvent {
  q: string
  hitKind: 'task' | 'note' | 'blob'
  hitId: string
  /** 1-based position in the ranked result list. */
  rank: number
  /** How many ranked hits the list held, so a rank can be read as a
   *  fraction rather than as an absolute. */
  resultCount: number
}

export interface SearchClickBody {
  q: string
  hit_kind: string
  hit_id: string
  rank: number
  result_count: number
}

export const SEARCH_CLICK_PATH = '/search/click'

export function searchClickBody(ev: SearchClickEvent): SearchClickBody {
  return {
    q: ev.q,
    hit_kind: ev.hitKind,
    hit_id: ev.hitId,
    rank: ev.rank,
    result_count: ev.resultCount,
  }
}
