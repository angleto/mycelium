import { describe, expect, it } from 'vitest'
import { decideBuildAction } from './useBuildWatch'

// The reload policy is the part of the build watcher that can hurt: get
// it wrong in one direction and the user keeps hard-reloading by hand,
// get it wrong in the other and an automatic reload eats text they were
// still typing. Both directions are asserted here, on the pure decision
// so no DOM, timer or fetch is involved.

const OLD = 'sha-old'
const NEW = 'sha-new'

function decide(over: Partial<Parameters<typeof decideBuildAction>[0]> = {}) {
  return decideBuildAction({
    served: NEW,
    current: OLD,
    unsaved: false,
    reloadedFor: null,
    ...over,
  })
}

describe('decideBuildAction', () => {
  it('reloads when the server serves a different build and nothing is unsaved', () => {
    expect(decide()).toBe('reload')
  })

  it('does nothing when the served build is the running one', () => {
    expect(decide({ served: OLD })).toBe('none')
  })

  it('never reloads over unsaved edits — it asks instead', () => {
    expect(decide({ unsaved: true })).toBe('banner')
  })

  it('does not loop when a reload for this exact build already happened', () => {
    // A rolling update can serve two bundles: without this, every flip
    // of /version.json would trigger another reload and the app would
    // never settle.
    expect(decide({ reloadedFor: NEW })).toBe('banner')
  })

  it('still reloads for a build newer than the one already reloaded for', () => {
    expect(decide({ reloadedFor: 'sha-older' })).toBe('reload')
  })

  it('stays silent when either side is unknown', () => {
    // Unreachable /version.json (offline, mid-deploy 404) and a bundle
    // built without the define must both degrade to "never interrupt",
    // not to a spurious reload.
    expect(decide({ served: null })).toBe('none')
    expect(decide({ current: null })).toBe('none')
    expect(decide({ served: null, unsaved: true })).toBe('none')
  })
})
