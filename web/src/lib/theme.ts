// Theme: 'auto' follows the OS (no data-theme attribute → the CSS
// prefers-color-scheme media query governs); 'light'/'dark' force it
// via html[data-theme]. Persisted in localStorage; applied before
// React renders (main.tsx) to avoid a flash.
export type Theme = 'auto' | 'light' | 'dark'

const KEY = 'mycelium-theme'

export function getTheme(): Theme {
  const v = localStorage.getItem(KEY)
  return v === 'light' || v === 'dark' ? v : 'auto'
}

export function applyTheme(theme: Theme): void {
  const root = document.documentElement
  if (theme === 'auto') root.removeAttribute('data-theme')
  else root.setAttribute('data-theme', theme)
}

export function setTheme(theme: Theme): void {
  if (theme === 'auto') localStorage.removeItem(KEY)
  else localStorage.setItem(KEY, theme)
  applyTheme(theme)
}

export function initTheme(): void {
  applyTheme(getTheme())
}
