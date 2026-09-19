import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { decideChangeAction, shouldProbe, useChangeWatch } from './useChangeWatch'

type Probe_ = (since: string | null) => Promise<{ since: string; changes: number } | null>

// The two decisions inside the collection watcher, asserted on the pure
// functions so no DOM, timer or fetch is involved. Both can hurt in a way
// the type-checker cannot see: probe too eagerly and the refresh lands on
// top of a gesture the user is still making; read the count wrongly and the
// view either never refreshes or refreshes on every tick forever.
//
// Observed failing (2026-09-19) against the two shapes this was written to
// rule out: dropping the drag guard from shouldProbe, and reading the count
// as "non-zero means changed" instead of "different from the baseline".
// Three assertions went red -- the drag one, the bootstrap one, and the
// steady state, which is the refresh-on-every-tick-forever case. A third
// perturbation, moving the re-baseline to AFTER the re-read, was seen red
// only once the loop test below logged both sides in one list: the first
// version of it asserted the same calls without their order and passed
// against the swap.

describe('shouldProbe', () => {
  const ok = { visible: true, dragging: false, selectFocused: false, inflight: false }

  it('probes a visible, idle tab', () => {
    expect(shouldProbe(ok)).toBe(true)
  })

  it('stays quiet in a hidden tab', () => {
    expect(shouldProbe({ ...ok, visible: false })).toBe(false)
  })

  it('never interrupts a drag', () => {
    // A board refresh mid-drag loses the drop: the card being dragged is
    // re-rendered from the server's copy, which is still in the old column.
    expect(shouldProbe({ ...ok, dragging: true })).toBe(false)
  })

  it('never interrupts an open select', () => {
    // The state select is on every list row; a re-render closes it under
    // the pointer, so the click that follows lands on nothing.
    expect(shouldProbe({ ...ok, selectFocused: true })).toBe(false)
  })

  it('does not stack probes', () => {
    expect(shouldProbe({ ...ok, inflight: true })).toBe(false)
  })
})

describe('decideChangeAction', () => {
  const probed = { since: '2026-09-19T10:00:00Z', changes: 3 }

  it('takes the first answer as a baseline and refreshes nothing', () => {
    // The bootstrap window reaches a couple of seconds into the past, so a
    // non-zero first count is normal and describes a past the view was
    // already rendered from. Treating it as a change would refresh every
    // view the moment it opened.
    expect(decideChangeAction({ bootstrapped: false, baseline: 0, probed })).toBe('baseline')
  })

  it('refreshes when the count moves', () => {
    expect(decideChangeAction({ bootstrapped: true, baseline: 2, probed })).toBe('refresh')
  })

  it('does nothing while the count stands still', () => {
    // The steady state, and the one that must cost nothing: a quiet
    // workspace answers the same number forever.
    expect(decideChangeAction({ bootstrapped: true, baseline: 3, probed })).toBe('none')
  })

  it('holds its ground when the probe fails', () => {
    // A blip must not look like a change (a refresh storm) nor reset the
    // baseline (which would swallow the next real one).
    expect(decideChangeAction({ bootstrapped: true, baseline: 2, probed: null })).toBe('none')
    expect(decideChangeAction({ bootstrapped: false, baseline: 0, probed: null })).toBe('none')
  })

  it('reacts to a count that went DOWN, not only up', () => {
    // It can: the server counts a window that starts at a fixed instant,
    // and retention or a purge can remove rows from it. What matters is
    // that the number is not the one the view was read at.
    expect(decideChangeAction({ bootstrapped: true, baseline: 9, probed })).toBe('refresh')
  })
})

// The loop itself: the two pure decisions above say WHAT to do, this says
// that the hook does it -- one bootstrap, no refresh while the count stands
// still, and a re-baseline that happens BEFORE the view is re-read (the
// order that decides whether a write landing between the two is seen or
// lost). Driven with a stub probe rather than a stubbed fetch, because the
// transport is the caller's to supply and this is not the place to pin it.

describe('useChangeWatch', () => {
  let host: HTMLDivElement
  let root: Root

  function Probe({ probe, onChange }: { probe: Probe_; onChange: () => void }) {
    useChangeWatch({ enabled: true, resetKey: 'ws', probe, onChange, intervalMs: 1000 })
    return null
  }

  beforeEach(() => {
    vi.useFakeTimers()
    host = document.createElement('div')
    document.body.appendChild(host)
    root = createRoot(host)
  })

  afterEach(() => {
    act(() => root.unmount())
    host.remove()
    vi.useRealTimers()
  })

  async function tick(): Promise<void> {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000)
    })
  }

  it('bootstraps once, then refreshes only when the count moves', async () => {
    const answers = [
      { since: 'T0', changes: 2 }, // bootstrap: the baseline, not a change
      { since: 'T0', changes: 2 }, // quiet
      { since: 'T0', changes: 5 }, // something happened
      { since: 'T1', changes: 0 }, // the re-baseline that follows it
      { since: 'T1', changes: 0 }, // quiet again, at the new baseline
    ]
    // One log for both sides, because the ORDER between them is half of
    // what is being asserted: a re-baseline taken after the re-read would
    // swallow anything that landed in between.
    const log: string[] = []
    const probe = (since: string | null) => {
      log.push(`probe:${since ?? 'bootstrap'}`)
      return Promise.resolve(answers.shift() ?? null)
    }
    await act(async () => {
      root.render(<Probe probe={probe} onChange={() => log.push('refresh')} />)
    })
    expect(log).toEqual(['probe:bootstrap'])

    await tick()
    expect(log).toEqual(['probe:bootstrap', 'probe:T0'])

    await tick()
    expect(log).toEqual([
      'probe:bootstrap',
      'probe:T0',
      'probe:T0',
      'probe:bootstrap',
      'refresh',
    ])

    await tick()
    // Quiet again, and now measuring from the instant the re-baseline
    // returned rather than from the original one.
    expect(log.slice(5)).toEqual(['probe:T1'])
  })

  it('does not probe a hidden tab', async () => {
    const spy = vi.fn(() => Promise.resolve({ since: 'T0', changes: 0 }))
    vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden')
    await act(async () => {
      root.render(<Probe probe={spy} onChange={() => {}} />)
    })
    await tick()
    expect(spy).not.toHaveBeenCalled()
  })
})
