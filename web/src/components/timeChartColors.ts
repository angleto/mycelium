
import { useIsDark } from '../lib/useIsDark'

// The colour system behind the Time view's charts. It lives in its own
// module (not in TimeCharts.tsx) because the repo's react-refresh rule
// forbids a component file from also exporting constants and hooks — and
// the split is the right shape anyway: ONE definition of the ramp and of
// the key -> colour mapping, imported by the donut, the histogram and the
// legend alike.

// One ordered series list feeds every chart in the Time view: the donut,
// the per-day histogram and the shared legend all read the SAME array and
// the SAME key -> colour map, so a colour means one entity everywhere and
// dropping a series never repaints the survivors.
export interface ChartSeries {
  key: string
  label: string
  secs: number
}

// A single day's stack: seconds per series key. Keys absent from `parts`
// are zero — an all-zero day is still a column (it reads as "no work").
export interface DayBucket {
  day: string
  parts: Record<string, number>
}

// Categorical ramp, 8 slots: moss · info-blue · clay · violet · bark ·
// teal · gold · bloom. Brand-led ordering (the six app hue tokens plus
// teal and gold), NOT an arbitrary rainbow, and every slot stays clear of
// the reserved status tokens --err / --ok.
//
// Both ramps were machine-checked with the dataviz validator against
// their own surface (light #fdfcf7, dark #161e18) in adjacent-pair mode,
// the right pairlist for a stacked column chart and a donut. All checks
// pass in both themes:
//   light  CVD ΔE 13.2 (deutan) · normal ΔE 16.5 · all >= 3:1 vs surface
//   dark   CVD ΔE 12.0 (deutan) · normal ΔE 17.0 · all >= 3:1 vs surface
// The stacked bars DEPEND on that separation: neighbouring segments in a
// column are told apart by hue plus the 2px surface gap, nothing else.
// Do not reorder or hand-tweak a step without re-running the validator.
export const CHART_LIGHT = [
  '#356a20',
  '#235b9c',
  '#bf4e3a',
  '#6d4cad',
  '#8b5002',
  '#189e8c',
  '#8a7310',
  '#cd759d',
] as const
export const CHART_DARK = [
  '#70a356',
  '#5497d9',
  '#d26e5b',
  '#9e80d1',
  '#bd8130',
  '#0baa92',
  '#af8f15',
  '#c8729c',
] as const

// Sentinel key for the folded tail ("+N"). A 9th categorical hue is never
// generated: past 8 the tail folds into one residual bucket, and that
// bucket is deliberately ACHROMATIC so it reads as "everything else"
// rather than as a peer category. Both residual steps were searched
// against all 8 slots with the same validator: they clear the CVD target
// (light 13.4, dark 9.0), the normal-vision floor (16.7 / 17.9) and 3:1
// against their surface (11.0 / 3.1), including the donut's wrap pair
// back to slot 1. They intentionally fail the chroma floor — that is the
// "not a category" signal — and, in light, the categorical lightness band
// (the residual sits below it so it never competes with a real series).
export const REST_KEY = ' rest'
const REST_LIGHT = '#3d3a35'
const REST_DARK = '#686868'

/** The 8-slot categorical ramp for the resolved theme. A single fixed set
 * cannot read against both a near-white and a near-black surface, so dark
 * mode is its own SELECTED set of steps, not an automatic flip. */
export function useChartColors(): readonly string[] {
  return useIsDark() ? CHART_DARK : CHART_LIGHT
}

// How many past assignments to remember. Bounded because grouping by task
// can walk through a lot of distinct keys in one session; the map keeps
// insertion order, so trimming from the front drops the least recent.
const SLOT_MEMORY = 200

// Which ramp slot each entity was given, remembered for the life of the
// tab. Deliberately a module-level store rather than React state: nothing
// re-renders when it changes (the render below derives the assignment
// itself, so a newcomer is already correct on its first frame) and it is
// shared by every chart that asks about the same entity. As React state it
// would need a setState inside an effect — a cascading render for data the
// UI never reads directly.
const slotMemory = new Map<string, number>()

/** Build the ONE key -> colour map both charts share.
 *
 * Colour follows the ENTITY, not its rank. That distinction is the whole
 * point: `series` is sorted by tracked time and narrowed by the report's
 * client / project selector, so indexing the ramp by position repaints
 * every survivor the moment one bucket drops out — filter to a single
 * client and the project you were reading changes colour under you, which
 * is exactly the confusion the shared legend is supposed to prevent.
 *
 * So a key KEEPS the slot it was first given for as long as it is on
 * screen, and a slot is only reused once its key is gone. Newcomers take
 * the lowest free slot in size order, so a fresh view still opens on the
 * brand's leading hues. Re-running with the same input yields the same
 * map (remembered keys re-take their own slots), so a double render in
 * StrictMode cannot shuffle the colours. */
function assignSlots(
  keys: readonly string[],
  remembered: ReadonlyMap<string, number>,
  slotCount: number,
): Map<string, number> {
  const taken = new Set<number>()
  const out = new Map<string, number>()
  // Pass 1: every key that already has a slot keeps it.
  for (const k of keys) {
    const prev = remembered.get(k)
    if (prev !== undefined && prev < slotCount && !taken.has(prev)) {
      taken.add(prev)
      out.set(k, prev)
    }
  }
  // Pass 2: newcomers take the lowest slot still free, in the order given
  // (size order), so a fresh view opens on the brand's leading hues.
  for (const k of keys) {
    if (out.has(k)) continue
    let slot = 0
    while (slot < slotCount && taken.has(slot)) slot += 1
    // Never cycle the ramp: past the last slot the bucket is the residual.
    if (slot >= slotCount) continue
    taken.add(slot)
    out.set(k, slot)
  }
  return out
}

export function useColorOf(series: ChartSeries[]): (key: string) => string {
  const dark = useIsDark()
  const ramp = dark ? CHART_DARK : CHART_LIGHT
  const rest = dark ? REST_DARK : REST_LIGHT
  // The residual never consumes a ramp slot: it is achromatic by design.
  const liveKeys = series.filter((s) => s.key !== REST_KEY).map((s) => s.key)
  const assigned = assignSlots(liveKeys, slotMemory, ramp.length)
  // Recording the assignment is idempotent — re-running with the same keys
  // reproduces it exactly, because pass 1 hands every remembered key its
  // own slot back — so a double render cannot shuffle the colours. A
  // discarded concurrent render can leave a slot remembered for an entity
  // that was never painted; the cost of that is nil.
  for (const [k, slot] of assigned) slotMemory.set(k, slot)
  if (slotMemory.size > SLOT_MEMORY) {
    // Map keeps insertion order, so the head is the least recently added.
    for (const k of [...slotMemory.keys()].slice(0, slotMemory.size - SLOT_MEMORY)) {
      if (!assigned.has(k)) slotMemory.delete(k)
    }
  }
  return (key: string) => {
    const slot = assigned.get(key)
    return slot === undefined ? rest : ramp[slot]
  }
}
