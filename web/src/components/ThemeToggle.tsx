import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { getTheme, setTheme, type Theme } from '../lib/theme'

// Tri-state toggle (like bitvision): cycle auto → light → dark → auto.
// The shown glyph is the CURRENT mode: sun=light, moon=dark,
// half-moon (half filled)=auto/system.
const ORDER: Theme[] = ['auto', 'light', 'dark']

function Glyph({ theme }: { theme: Theme }) {
  if (theme === 'light') {
    return (
      <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
        <circle
          cx="12"
          cy="12"
          r="4.2"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.8"
        />
        <g stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
          <path d="M12 2.5v2.6M12 18.9v2.6M2.5 12h2.6M18.9 12h2.6M5 5l1.9 1.9M17.1 17.1 19 19M19 5l-1.9 1.9M6.9 17.1 5 19" />
        </g>
      </svg>
    )
  }
  if (theme === 'dark') {
    return (
      <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
        <path
          d="M20 13.5A8 8 0 1 1 10.5 4a6.5 6.5 0 0 0 9.5 9.5Z"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinejoin="round"
        />
      </svg>
    )
  }
  // auto: a moon split half-filled / half-outline.
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
      <circle
        cx="12"
        cy="12"
        r="8"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
      />
      <path d="M12 4a8 8 0 0 0 0 16Z" fill="currentColor" />
    </svg>
  )
}

export function ThemeToggle() {
  const { t } = useTranslation()
  const [theme, setThemeState] = useState<Theme>(getTheme())
  const label =
    theme === 'light'
      ? t('settings.themeLight')
      : theme === 'dark'
        ? t('settings.themeDark')
        : t('settings.themeAuto')
  return (
    <button
      type="button"
      className="btn--ghost btn--sm themetoggle"
      title={`${t('settings.theme')}: ${label}`}
      aria-label={`${t('settings.theme')}: ${label}`}
      onClick={() => {
        const next = ORDER[(ORDER.indexOf(theme) + 1) % ORDER.length]
        setTheme(next)
        setThemeState(next)
      }}
    >
      <Glyph theme={theme} />
    </button>
  )
}
