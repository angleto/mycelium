// Where a note sits on the garden board and where a person may move it
// (task e584ed61). Kept apart from the component so the list's tabs
// (GardenRoute.bucketOf) and the board read one placement rule. Why only
// growing and mature take a card is written in GardenKanban.tsx, beside
// the view it shapes.
import type { components } from '../shared'

type Note = components['schemas']['NoteListOut']

export type GardenColumn = 'seed' | 'growing' | 'mature' | 'dormant'

export const GARDEN_COLUMNS: readonly GardenColumn[] = ['seed', 'growing', 'mature', 'dormant']

// The columns a card may be dropped into, from any column. A move into
// seed or dormant is undone by the maturity sweep (a recent touch turns
// either into growing, and the move is itself a touch). A move into growing
// or mature stands: the sweep ages it to dormant only after 60 untouched
// days, and the value-axis auto-promotion (garden_classify
// .auto_promote_mature) leaves alone a note a person took out of mature
// until something moves it again (note_links.person_held_maturity), so a
// card taken from mature back to growing is not matured again behind the
// person's back. A card moved from seed or dormant into growing holds
// nothing: the auto-promotion may mature it later, as after the sweep.
export const DROP_COLUMNS: ReadonlySet<GardenColumn> = new Set(['growing', 'mature'])

/** The column a note sits in. A transplanted note goes with the dormant
 * ones, where the list puts it too (the Deadwood tab holds "the dormant and
 * the transplanted"): it became a task and is read-only, so it sits with
 * the notes that are no longer growing, whatever maturity it carried when
 * it was promoted. A stage this view does not know is drawn as growing,
 * the Garden tab, where the list filed it before the board existed. */
export function columnOf(n: Note): GardenColumn {
  if (n.promoted_at) return 'dormant'
  if (n.maturity === 'seed' || n.maturity === 'mature' || n.maturity === 'dormant') {
    return n.maturity
  }
  return 'growing'
}

/** The columns a person may move this card to: growing and mature, less
 * the stage it already has. None for a transplanted note, which is
 * read-only on the server too.
 *
 * Compared with the note's real maturity, not with the column it is drawn
 * in: a stage this view does not know is drawn as growing, and both growing
 * and mature are moves that give it a known stage. */
export function moveTargetsOf(n: Note): GardenColumn[] {
  if (n.promoted_at) return []
  return GARDEN_COLUMNS.filter((c) => DROP_COLUMNS.has(c) && c !== n.maturity)
}
