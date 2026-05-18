import { useEffect, useState } from 'react'
import { NavLink, Outlet, useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api, workspaceHeader } from '../api/client'
import { clearSession } from '../auth/session'
import { useSession } from '../auth/useSession'
import { Logo } from './Logo'
import { Icon, type IconName } from './NavIcon'
import { ThemeToggle } from './ThemeToggle'
import { hms, elapsedSec } from '../lib/time'
import { parseMentionHref, routeForMention } from '../lib/mentions'
import { useFocus } from '../lib/focus'
import type { components } from '../api/schema'
import i18n from '../i18n'

type Item = { to: string; label: string; icon: IconName }
type Running = components['schemas']['TimeEntryOut']
type Tag = components['schemas']['TagOut']
type Project = components['schemas']['ProjectOut']

// Focus: pick a client (all its projects) and optionally narrow to one
// project. The project-scoped views (Notes, Tasks) filter accordingly.
function ProjectFocus() {
  const { t } = useTranslation()
  const session = useSession()
  const { clientId, projectId, setClient, setProject, setClientProjectIds } =
    useFocus()
  const [clients, setClients] = useState<Tag[]>([])
  const [projects, setProjects] = useState<Project[]>([])

  useEffect(() => {
    let active = true
    void (async () => {
      const h = workspaceHeader()
      const [c, p] = await Promise.all([
        api.GET('/tags', { params: { header: h, query: { kind: 'client' } } }),
        api.GET('/projects', { params: { header: h } }),
      ])
      if (!active) return
      if (c.data) setClients(c.data)
      if (p.data) setProjects(p.data)
    })()
    return () => {
      active = false
    }
  }, [session?.workspaceId])

  const ofClient = projects.filter((p) => p.client_tag_id === clientId)
  // Keep the provider's effective allow-list in sync with the data.
  useEffect(() => {
    setClientProjectIds(
      clientId
        ? projects
            .filter((p) => p.client_tag_id === clientId)
            .map((p) => p.id)
        : [],
    )
  }, [clientId, projects, setClientProjectIds])

  return (
    <div className="focus">
      <span className="focus__lbl">{t('focus.label')}</span>
      <select value={clientId} onChange={(e) => setClient(e.target.value)}>
        <option value="">{t('focus.allClients')}</option>
        {clients.map((c) => (
          <option key={c.id} value={c.id}>
            {c.name}
          </option>
        ))}
      </select>
      {clientId && (
        <select
          value={projectId}
          onChange={(e) => setProject(e.target.value)}
        >
          <option value="">{t('focus.allOfClient')}</option>
          {ofClient.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
      )}
    </div>
  )
}

// Top-bar running indicator. One timer: spinner + title + live
// elapsed. Several: cycles every 5s through them showing "i/n", the
// task title (scrolling) and that timer's live elapsed. Polls
// /time/running on the timer views' 5s cadence.
function RunningIndicator() {
  const { t } = useTranslation()
  const session = useSession()
  const [runs, setRuns] = useState<Running[]>([])
  const [titles, setTitles] = useState<Record<string, string>>({})
  const [now, setNow] = useState(() => Date.now())
  const [idx, setIdx] = useState(0)

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

  // Resolve task titles for the running entries (TimeEntryOut has only
  // task_id). One list fetch, cached by id.
  useEffect(() => {
    let active = true
    void (async () => {
      const { data } = await api.GET('/tasks', {
        params: { header: workspaceHeader() },
      })
      if (active && data) {
        setTitles(Object.fromEntries(data.map((tk) => [tk.id, tk.title])))
      }
    })()
    return () => {
      active = false
    }
  }, [session?.workspaceId])

  useEffect(() => {
    if (runs.length === 0) return
    const clock = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(clock)
  }, [runs.length])

  // Cycle through the running entries every 5s when there is >1.
  // (idx is bounded at render via safeIdx, so no reset needed here.)
  useEffect(() => {
    if (runs.length <= 1) return
    const c = setInterval(
      () => setIdx((i) => (i + 1) % runs.length),
      5000,
    )
    return () => clearInterval(c)
  }, [runs.length])

  if (runs.length === 0) return null
  const ordered = [...runs].sort(
    (a, b) =>
      new Date(a.started_at).getTime() - new Date(b.started_at).getTime(),
  )
  const safeIdx = idx % ordered.length
  const cur = ordered[safeIdx]
  const title = titles[cur.task_id] ?? cur.task_id.slice(0, 8)
  return (
    <div className="running" title={t('time.runningNow')}>
      <span className="running__spin" aria-hidden="true" />
      <span className="running__n">
        {safeIdx + 1}/{ordered.length}
      </span>
      <span className="running__title">
        <span>{title}</span>
      </span>
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
  const navigate = useNavigate()

  // Mention links (@kind:id) are stored as plain markdown. MarkdownView
  // renders them as router Links, but the tiptap editor renders a raw
  // <a href="@note:id"> which the browser would resolve to a broken
  // /tasks/@note:... URL. One capture-phase interceptor routes ANY such
  // anchor app-side — no per-view duplication.
  useEffect(() => {
    const onClick = (e: MouseEvent) => {
      const a = (e.target as HTMLElement | null)?.closest('a')
      const href = a?.getAttribute('href')
      if (!href) return
      const m = parseMentionHref(href)
      if (!m) return
      e.preventDefault()
      navigate(routeForMention(m.kind, m.id))
    }
    document.addEventListener('click', onClick, true)
    return () => document.removeEventListener('click', onClick, true)
  }, [navigate])

  async function onLogout() {
    // Real server-side logout: revoke the JWT, then drop the session.
    await api.POST('/auth/logout')
    clearSession()
  }

  const groups: { title: string; items: Item[] }[] = [
    {
      title: t('nav.groups.knowledge'),
      items: [
        { to: '/notes', label: t('notes.nav'), icon: 'notes' },
        { to: '/memory', label: t('memory.nav'), icon: 'memory' },
      ],
    },
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
            <ProjectFocus />
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
