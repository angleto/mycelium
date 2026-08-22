import { useEffect, useRef } from 'react'

/**
 * App-wide "is anything unsaved right now?" register.
 *
 * The reason this exists: an automatic reload onto a new frontend build
 * (see useBuildWatch) is a page teardown, and this app lets you type into
 * a task or a note for minutes before the debounce writes. Reloading
 * under that would destroy work silently — the same failure the
 * no-auto-merge 409 policy and RefreshHint were built to avoid. So the
 * reload asks first, and this is what it asks.
 *
 * Deliberately a module-level Set rather than context: the asker is not a
 * React component in the editors' tree (it runs from a timer/visibility
 * event), and the answer must be readable synchronously at the instant
 * the decision is taken, not one render later.
 */
const dirtyOwners = new Set<object>()

/** True when at least one mounted editor reports unsaved changes. */
export function hasUnsavedEdits(): boolean {
  return dirtyOwners.size > 0
}

/**
 * Declare this component's unsaved state for the lifetime of the mount.
 *
 * Call it unconditionally with the editor's own `dirty` flag; the entry
 * is dropped on unmount, so a route change can never strand a stale
 * "dirty" that blocks every future reload.
 */
export function useUnsavedGuard(dirty: boolean): void {
  // Identity of this mount. A ref (not a key string) so two editors of
  // the same entity, or StrictMode's double-mount, can never collide or
  // delete each other's entry.
  const owner = useRef<object>({})
  useEffect(() => {
    const me = owner.current
    if (!dirty) {
      dirtyOwners.delete(me)
      return
    }
    dirtyOwners.add(me)
    // The cleanup covers BOTH transitions out of dirty: the flag going
    // false, and the component unmounting while still dirty. One effect
    // rather than two, so there is no ordering between them to get
    // wrong under StrictMode's double-mount.
    return () => {
      dirtyOwners.delete(me)
    }
  }, [dirty])
}

/** Test seam: drop every registration. Never call from app code. */
export function __resetUnsavedGuard(): void {
  dirtyOwners.clear()
}
