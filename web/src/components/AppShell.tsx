import { Outlet } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { clearSession } from '../auth/session'
import i18n from '../i18n'

export function AppShell() {
  const { t } = useTranslation()
  return (
    <div className="shell">
      <header className="shell__bar">
        <span className="shell__brand">{t('app.title')}</span>
        <div className="shell__actions">
          <label className="shell__lang">
            {t('nav.language')}{' '}
            <select
              value={i18n.language}
              onChange={(e) => void i18n.changeLanguage(e.target.value)}
            >
              <option value="en">EN</option>
              <option value="it">IT</option>
            </select>
          </label>
          <button type="button" onClick={() => clearSession()}>
            {t('nav.logout')}
          </button>
        </div>
      </header>
      <main className="shell__main">
        <Outlet />
      </main>
    </div>
  )
}
