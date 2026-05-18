import { Link, Outlet } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api } from '../api/client'
import { clearSession } from '../auth/session'
import { WorkspaceSwitcher } from './WorkspaceSwitcher'
import i18n from '../i18n'

export function AppShell() {
  const { t } = useTranslation()

  async function onLogout() {
    // Real server-side logout: revoke the JWT, then drop the session.
    await api.POST('/auth/logout')
    clearSession()
  }

  return (
    <div className="shell">
      <header className="shell__bar">
        <Link to="/" className="shell__brand">
          {t('app.title')}
        </Link>
        <WorkspaceSwitcher />
        <div className="shell__actions">
          <Link to="/tasks">{t('tasks.nav')}</Link>
          <Link to="/workflows">{t('workflows.nav')}</Link>
          <Link to="/graph">{t('graph.nav')}</Link>
          <Link to="/schedule">{t('scheduler.nav')}</Link>
          <Link to="/calendar">{t('events.nav')}</Link>
          <Link to="/settings">{t('nav.settings')}</Link>
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
          <button type="button" onClick={() => void onLogout()}>
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
