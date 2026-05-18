import { useEffect, useState } from 'react'
import { NavLink, Outlet } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api, workspaceHeader } from '../api/client'
import { clearSession } from '../auth/session'
import { useSession } from '../auth/useSession'
import { WorkspaceSwitcher } from './WorkspaceSwitcher'
import { Logo } from './Logo'
import { Icon, type IconName } from './NavIcon'
import { ThemeToggle } from './ThemeToggle'
import { hms, elapsedSec } from '../lib/time'
import type { components } from '../api/schema'
import i18n from '../i18n'

type Item = { to: string; label: string; icon: IconName }
type Running = components['schemas']['TimeEntryOut']

// Top-bar running indicator: a slow spinner shown only while a timer
// runs, the count of running timers, and the live elapsed of the
// current (most recently started) one, ticking each second. Polls
// /time/running on the same 5s cadence as the timer views.
function RunningIndicator() {
  const { t } = useTranslation()
  const session = useSession()
  const [runs, setRuns] = useState<Running[]>([])
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    let active = true
    const tick = async () => {
      const { data } = await api.GET('/time/running', {
        params: { header: workspaceHeader() },
      })
      if (active) setRuns(data ?? [])
    }
    void tick()
    const poll = setInterval(() => void tick(), 5000)
    return () => {
      active = false
      clearInterval(poll)
    }
  }, [session?.workspaceId])

  useEffect(() => {
    if (runs.length === 0) return
    const clock = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(clock)
  }, [runs.length])

  if (runs.length === 0) return null
  const cur = [...runs].sort(
    (a, b) =>
      new Date(b.started_at).getTime() - new Date(a.started_at).getTime(),
  )[0]
  return (
    <div className="running" title={t('time.runningNow')}>
      <span className="running__spin" aria-hidden="true" />
      <span className="running__n">{runs.length}</span>
      <span className="running__t">{hms(elapsedSec(cur.started_at, now))}</span>
    </div>
  )
}

function NavGroup({ title, items }: { title: string; items: Item[] }) {
  return (
    <div className="nav__group">
      <div className="nav__title">{title}</div>
      {items.map((it) => (
        <NavLink
          key={it.to}
          to={it.to}
          end={it.to === '/'}
          className={({ isActive }) =>
            isActive ? 'nav__link nav__link--active' : 'nav__link'
          }
        >
          <Icon name={it.icon} />
          {it.label}
        </NavLink>
      ))}
    </div>
  )
}

export function AppShell() {
  const { t } = useTranslation()

  async function onLogout() {
    // Real server-side logout: revoke the JWT, then drop the session.
    await api.POST('/auth/logout')
    clearSession()
  }

  const groups: { title: string; items: Item[] }[] = [
    {
      title: t('nav.groups.productivity'),
      items: [
        { to: '/', label: t('home.title'), icon: 'home' },
        { to: '/tasks', label: t('tasks.nav'), icon: 'tasks' },
        { to: '/time', label: t('time.nav'), icon: 'time' },
        { to: '/schedule', label: t('scheduler.nav'), icon: 'schedule' },
        { to: '/calendar', label: t('events.nav'), icon: 'calendar' },
        { to: '/trash', label: t('trash.nav'), icon: 'trash' },
      ],
    },
    {
      title: t('nav.groups.structure'),
      items: [
        { to: '/workflows', label: t('workflows.nav'), icon: 'workflows' },
        { to: '/graph', label: t('graph.nav'), icon: 'graph' },
        { to: '/tags', label: t('tagmgr.nav'), icon: 'tags' },
        { to: '/clients', label: t('cp.nav'), icon: 'clients' },
      ],
    },
    {
      title: t('nav.groups.planning'),
      items: [
        { to: '/advisory', label: t('advisory.nav'), icon: 'advisory' },
        { to: '/budgets', label: t('budgets.nav'), icon: 'budgets' },
      ],
    },
    {
      title: t('nav.groups.knowledge'),
      items: [
        { to: '/notes', label: t('notes.nav'), icon: 'notes' },
        { to: '/memory', label: t('memory.nav'), icon: 'memory' },
      ],
    },
    {
      title: t('nav.groups.comms'),
      items: [
        { to: '/email', label: t('email.nav'), icon: 'email' },
        { to: '/notifications', label: t('notif.nav'), icon: 'notifications' },
      ],
    },
    {
      title: t('nav.groups.billing'),
      items: [
        { to: '/billing', label: t('billing.nav'), icon: 'billing' },
        { to: '/invoices', label: t('invoices.nav'), icon: 'invoices' },
      ],
    },
  ]

  return (
    <div className="app">
      <header className="topbar">
        <div className="topbar__brand">
          <Logo /> {t('app.title')}
        </div>
        <div className="topbar__actions">
          <RunningIndicator />
          <ThemeToggle />
          <select
            aria-label="language"
            value={i18n.language}
            onChange={(e) => void i18n.changeLanguage(e.target.value)}
          >
            <option value="en">EN</option>
            <option value="it">IT</option>
          </select>
          <NavLink
            to="/settings"
            className={({ isActive }) =>
              isActive ? 'nav__link nav__link--active' : 'nav__link'
            }
          >
            <Icon name="settings" />
            {t('nav.settings')}
          </NavLink>
          <button
            type="button"
            className="btn btn--ghost"
            onClick={() => void onLogout()}
          >
            {t('nav.logout')}
          </button>
        </div>
      </header>
      <div className="layout">
        <aside className="sidebar">
          <div className="sidebar__ws">
            <WorkspaceSwitcher />
          </div>
          <nav className="nav">
            {groups.map((g) => (
              <NavGroup key={g.title} title={g.title} items={g.items} />
            ))}
          </nav>
        </aside>
        <main className="content">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
