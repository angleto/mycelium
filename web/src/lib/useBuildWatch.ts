import { useEffect, useState } from 'react'
import { BUILD_ID } from 'virtual:mycelium-build-id'
import { hasUnsavedEdits } from './unsavedGuard'

/**
 * Detect that the server now serves a DIFFERENT frontend than the one
 * this tab is running, and get onto it without a manual hard reload.
 *
 * Why a poll and not the service worker's own update event: the SW
 * script only changes when someone edits sw.js, so `updatefound` fires
 * on service-worker releases, not on frontend releases. The question the
 * user actually has ("is my UI the deployed UI?") is answered by
 * `/version.json`, which the build emits fresh every time
 * (web/vite.config.ts).
 *
 * Wake triggers mirror useStaleWatch — tab focus / visibility / network
 * back — because "switch away, come back to a released app" is the
 * common shape. The slow interval covers the shape wake triggers cannot:
 * a tab left open and focused for hours while a rollout lands.
 */

/** Poll cadence while the tab is visible. Long on purpose: the wake
 * triggers already catch the interactive case, so this only bounds how
 * long a never-blurred tab can stay behind. */
const POLL_MS = 5 * 60_000

const VERSION_URL = '/version.json'

/** Marks that we already reloaded targeting a given build id. Guards the
 * pathological loop: during a rolling update two pods can serve different
 * bundles, so version.json can flip back and forth and each flip would
 * otherwise trigger another reload. sessionStorage (not local) so the
 * guard dies with the tab. */
const RELOAD_STAMP = 'mycelium.buildwatch.reloadedFor'

/** Identity of the running bundle (vite.config.ts emits the module).
 * Narrowed to null when absent so an id we cannot read degrades to
 * "unknown" — decideBuildAction then never interrupts — instead of
 * comparing against undefined and claiming a change on every poll. */
export function currentBuildId(): string | null {
  return typeof BUILD_ID === 'string' && BUILD_ID ? BUILD_ID : null
}

export type BuildAction = 'none' | 'reload' | 'banner'

/**
 * What to do about the build id the server just reported. Pure, so the
 * policy can be asserted without a DOM, a timer or a fetch.
 *
 * The policy, and it is the whole design decision:
 *
 *   nothing unsaved  ->  reload immediately, no prompt. This is the
 *                        common case and what "automatic" has to mean to
 *                        be worth anything.
 *   unsaved edits    ->  never reload on our own. Surface the banner and
 *                        let the user finish the sentence they are
 *                        typing. Consistent with RefreshHint / the 409
 *                        no-auto-merge policy: this app does not discard
 *                        typed text to save the user a keystroke.
 *   already reloaded ->  banner, never a second reload. We jumped to this
 *   for this id           id and came back still running the old code, so
 *                         something upstream is inconsistent (a rolling
 *                         update serving two bundles). Looping on it
 *                         would make the app unusable; a human decides.
 */
export function decideBuildAction(opts: {
  /** What /version.json says now; null when unknown/unreachable. */
  served: string | null
  /** What this bundle was built as; null when unknown. */
  current: string | null
  unsaved: boolean
  /** Build id this tab already reloaded for, if any. */
  reloadedFor: string | null
}): BuildAction {
  const { served, current, unsaved, reloadedFor } = opts
  // Unknown on either side: we cannot substantiate a difference, so we
  // do not claim one. A missing /version.json (an old deploy, a dev
  // server without the plugin) must degrade to "never interrupt".
  if (!served || !current) return 'none'
  if (served === current) return 'none'
  if (reloadedFor === served) return 'banner'
  return unsaved ? 'banner' : 'reload'
}

async function fetchServedBuildId(): Promise<string | null> {
  try {
    const res = await fetch(VERSION_URL, {
      cache: 'no-store',
      credentials: 'omit',
    })
    if (!res.ok) return null
    const data: unknown = await res.json()
    const id = (data as { buildId?: unknown } | null)?.buildId
    return typeof id === 'string' && id ? id : null
  } catch {
    // Offline, a blip, or a deploy mid-swap. Say nothing rather than
    // claim a version change we cannot substantiate.
    return null
  }
}

/** Build id this tab already reloaded for. Storage denied (private mode,
 * blocked cookies) degrades to "no guard" rather than to "never
 * reload". */
export function readReloadStamp(): string | null {
  try {
    return sessionStorage.getItem(RELOAD_STAMP)
  } catch {
    return null
  }
}

function stampReloadFor(buildId: string): void {
  try {
    sessionStorage.setItem(RELOAD_STAMP, buildId)
  } catch {
    /* nothing to do; worst case we reload one extra time */
  }
}

/** Drop every service-worker cache, then reload. Without the drop, the
 * reload is answered by the SW — which since v6 is network-first for
 * navigations, so this is belt-and-braces for a client still carrying an
 * older worker at the moment of the switch. */
export async function reloadOntoNewBuild(buildId: string): Promise<void> {
  stampReloadFor(buildId)
  try {
    const reg = await navigator.serviceWorker?.getRegistration()
    reg?.active?.postMessage({ type: 'MYCELIUM_DROP_CACHE' })
    await reg?.update()
  } catch {
    /* no SW, or an unsupported browser: the reload still stands */
  }
  // `reload()` and not `location.href = location.href`: the latter is a
  // no-op on some browsers when the URL is unchanged.
  window.location.reload()
}

export interface BuildWatch {
  /** Build id the server serves, once known to differ from ours AND to
   * need a human (see decideBuildAction). Null in every other case. */
  newBuildId: string | null
  /** Apply it now (user-initiated escape hatch from the banner). */
  reloadNow: () => void
  /** Stop nagging for this build. */
  dismiss: () => void
}

/** Watch for a new frontend build; see decideBuildAction for the policy. */
export function useBuildWatch(): BuildWatch {
  const [newBuildId, setNewBuildId] = useState<string | null>(null)
  const [dismissed, setDismissed] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    let inflight = false

    const check = async () => {
      if (cancelled || inflight) return
      if (document.visibilityState !== 'visible') return
      inflight = true
      try {
        const served = await fetchServedBuildId()
        if (cancelled) return
        const action = decideBuildAction({
          served,
          current: currentBuildId(),
          unsaved: hasUnsavedEdits(),
          reloadedFor: readReloadStamp(),
        })
        if (action === 'banner') setNewBuildId(served)
        else if (action === 'reload') void reloadOntoNewBuild(served!)
      } finally {
        inflight = false
      }
    }

    const wake = () => void check()
    document.addEventListener('visibilitychange', wake)
    window.addEventListener('focus', wake)
    window.addEventListener('online', wake)
    const timer = window.setInterval(wake, POLL_MS)
    // No check on mount: the page just loaded from the server, so by
    // construction it runs what the server serves.
    return () => {
      cancelled = true
      document.removeEventListener('visibilitychange', wake)
      window.removeEventListener('focus', wake)
      window.removeEventListener('online', wake)
      window.clearInterval(timer)
    }
  }, [])

  return {
    newBuildId: newBuildId && newBuildId !== dismissed ? newBuildId : null,
    reloadNow: () => {
      if (newBuildId) void reloadOntoNewBuild(newBuildId)
    },
    dismiss: () => setDismissed(newBuildId),
  }
}
