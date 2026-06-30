import { useSyncExternalStore } from 'react'

// Subscribe a component to a CSS media query. Returns the current match
// and re-renders on change (resize, orientation). Uses
// useSyncExternalStore so there is no effect-driven flash and SSR/no-DOM
// renders fall back to `false` (mobile-drawer closed, desktop layout).
//
// One source of truth for the JS side of the responsive layout: the
// shell uses it to relocate the topbar utilities into the drawer below
// the layout breakpoint, and routes use it to pick a default view.
export function useMediaQuery(query: string): boolean {
  return useSyncExternalStore(
    (onChange) => {
      const mql = window.matchMedia(query)
      mql.addEventListener('change', onChange)
      return () => mql.removeEventListener('change', onChange)
    },
    () => window.matchMedia(query).matches,
    () => false,
  )
}

// The single layout breakpoint where the sidebar collapses into an
// off-canvas drawer and the topbar utilities move into it. Keep in sync
// with the `@media (max-width: 820px)` block in index.css.
export const MOBILE_QUERY = '(max-width: 820px)'
