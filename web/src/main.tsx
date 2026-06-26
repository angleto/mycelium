import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './i18n'
import './index.css'
import App from './App.tsx'
import { initTheme } from './lib/theme'
import { migrateLegacyStorageKeys } from './lib/legacyStorage'

migrateLegacyStorageKeys()
initTheme()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
