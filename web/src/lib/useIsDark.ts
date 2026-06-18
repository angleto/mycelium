import { useEffect, useState } from 'react'

// Effective light/dark, reacting both to the app's forced theme
// (html[data-theme], set by lib/theme.ts) and — when on 'auto' (no
// attribute) — the OS prefers-color-scheme. Shared by any view that picks
// a palette per theme in JS (Mermaid diagrams, the Time donut) so the
// colors never stay stale after a theme toggle.
export function readEffectiveTheme(): 'light' | 'dark' {
  const forced = document.documentElement.getAttribute('data-theme')
  if (forced === 'light' || forced === 'dark') return forced
  return window.matchMedia('(prefers-color-scheme: dark)').matches
    ? 'dark'
    : 'light'
}

export function useEffectiveTheme(): 'light' | 'dark' {
  const [theme, setTheme] = useState(readEffectiveTheme)
  useEffect(() => {
    const update = () => setTheme(readEffectiveTheme())
    const mq = window.matchMedia('(prefers-color-scheme: dark)')
    mq.addEventListener('change', update)
    const obs = new MutationObserver(update)
    obs.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ['data-theme'],
    })
    return () => {
      mq.removeEventListener('change', update)
      obs.disconnect()
    }
  }, [])
  return theme
}

export function useIsDark(): boolean {
  return useEffectiveTheme() === 'dark'
}
