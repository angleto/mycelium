import { useEffect, useState } from 'react'
import { Link, NavLink, Outlet, useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import {
  clearSession,
  getSession,
  setAdminMode,
  setWorkspaceRole,
} from '../auth/session'
import {
  useSession,
  useAdminMode,
  useWorkspaceRole,
} from '../auth/useSession'
import { useMe } from '../auth/useMe'
import { useMyWorkspace } from '../auth/useMyWorkspace'
import { Logo } from './Logo'
import { UserAvatar } from './UserAvatar'
import { Icon, type IconName } from './NavIcon'
import { ThemeToggle } from './ThemeToggle'
import { PomodoroTimer } from './PomodoroTimer'
import { MemoPopover } from './MemoPopover'
import { hms, activeElapsedSec, isPaused } from '../lib/time'
import { useRunningTimers, refreshRunning } from '../lib/useRunningTimer'
import {
  getCachedLookup,
  lookupPrefix,
  RESOLVE_ID,
  type LookupOut,
} from '../lib/prefixLookup'
import { CommandPalette } from './CommandPalette'
import { UpdateBanner } from './UpdateBanner'
import { useFocus } from '../lib/focus'
import { ClientSearch } from './ClientSearch'
import { WorkspaceSwitcher } from './WorkspaceSwitcher'
import { useMediaQuery, MOBILE_QUERY } from '../lib/useMediaQuery'
import type { components } from '../api/schema'
import i18n from '../i18n'

type Item = { to: string; label: string; icon: IconName }
type Project = components['schemas']['ProjectOut']

// Focus: pick a client (all its projects) and optionally narrow to one
// project. The project-scoped views (Notes, Tasks) filter accordingly.
function ProjectFocus() {
  const { t } = useTranslation()
  const session = useSession()
  const {
    clientId,
    projectId,
    clientName,
    setClient,
    setProject,
    setClientProjectIds,
    setProjectName,
  } = useFocus()
  const [projects, setProjects] = useState<Project[]>([])

  useEffect(() => {
    let active = true
    void (async () => {
      const h = workspaceHeader()
      // Projects only. The client list used to be fetched whole just to fill
      // a dropdown; with the search there is nothing to enumerate, and one
      // request per app load disappears with it.
      //
      // Archived projects are asked for explicitly: this list is both the
      // focus PICKER (which filters them out, below) and the project ->
      // name map used to label an ALREADY active focus. A focus record
      // survives its project being archived, and losing the name there
      // silently degrades the "Scoped to ..." chip to the client.
      const p = await api.GET('/projects', {
        params: { header: h, query: { include_archived: true } },
      })
      if (!active) return
      if (p.data) setProjects(p.data)
    })()
    return () => {
      active = false
    }
  }, [session?.workspaceId])

  // Archived projects never appear in the focus picker. /clients and
  // /projects both exclude them server-side now; this list opts back in
  // (see the fetch) for the name lookup below, so the picker filters them
  // out here.
  const visibleProjects = projects.filter((p) => p.status !== 'archived')
  const ofClient = visibleProjects.filter((p) => p.client_tag_id === clientId)
  // Keep the provider's effective allow-list in sync with the data.
  // Archived projects don't expand the focus scope.
  useEffect(() => {
    setClientProjectIds(
      clientId
        ? projects
            .filter((p) => p.client_tag_id === clientId && p.status !== 'archived')
            .map((p) => p.id)
        : [],
    )
  }, [clientId, projects, setClientProjectIds])
  // The project NAME is derived here because this is where the project list
  // lives; the client name travels with its id in the focus record.
  // Consumers (the advisory "Scoped to …" chip) read both off the context.
  useEffect(() => {
    const p = projects.find((x) => x.id === projectId)
    setProjectName(projectId ? (p?.name ?? '') : '')
  }, [projectId, projects, setProjectName])

  return (
    <div className="focus">
      <span className="focus__lbl">{t('focus.label')}</span>
      {/* A search, not a dropdown: one client per paying customer means this
          control is over an unbounded set, and enumerating it stops working
          long before the data does. Empty box = recently active clients.

          The displayed NAME comes from the picker rather than from a full
          client list -- with the search there IS no list in memory, and
          remembering it is what keeps the control from flashing "all clients"
          on reload. It is remembered WITH the id, in this workspace's focus
          record, so a name and an id from two different tenants can no longer
          be rendered together (task 805a569c). */}
      <ClientSearch
        currentName={clientId ? (clientName || '…') : ''}
        allLabel={t('focus.allClients')}
        placeholder={t('focus.allClients')}
        onChange={setClient}
      />
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
// task title (scrolling) and that timer's live elapsed. The title +
// elapsed are a link to the task currently shown; clicking the "i/n"
// badge advances to the next timer (wrapping) instead of navigating;
// the trailing ⏸/▶ and ■ controls pause/resume/stop the shown timer
// with the same server calls as TaskTimer. State comes from the
// shared server-authoritative source (useRunningTimers), which
// resyncs on resume from lid-close / reconnect / tab-switch.
function RunningIndicator() {
  const { t } = useTranslation()
  const { running: runs, now } = useRunningTimers()
  const [idx, setIdx] = useState(0)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  // Pause the carousel while a memo popover is open so the entry being
  // annotated is not swapped out from under the user mid-edit.
  const [editing, setEditing] = useState(false)

  // Auto-advance every 5s when there is >1. Keyed on idx so a manual
  // advance (badge click) re-arms the full 5s window before the next
  // automatic step. (idx is bounded at render via safeIdx.)
  useEffect(() => {
    if (runs.length <= 1 || editing) return
    const c = setTimeout(() => setIdx((i) => (i + 1) % runs.length), 5000)
    return () => clearTimeout(c)
  }, [runs.length, idx, editing])

  if (runs.length === 0) return null
  const ordered = [...runs].sort(
    (a, b) =>
      new Date(a.started_at).getTime() - new Date(b.started_at).getTime(),
  )
  const safeIdx = idx % ordered.length
  const cur = ordered[safeIdx]
  // The running entry already carries the server-resolved task title;
  // use it directly. A separate /tasks fetch missed tasks absent from
  // that (filtered/paginated) list — archived, done, or a note's subject
  // task — and fell back to the raw UUID prefix in the chip.
  const title = cur.task_title ?? cur.task_id.slice(0, 8)
  const paused = isPaused(cur)

  // Same server-authoritative protocol as TaskTimer: POST, then
  // reconcile via refreshRunning() so every consumer (this chip, the
  // TaskTimers) reflects server truth.
  async function stop() {
    setBusy(true)
    setErr(null)
    const { error } = await api.POST('/time/stop', {
      params: { header: workspaceHeader() },
      body: { task_id: cur.task_id },
    })
    setBusy(false)
    if (error) setErr(errMessage(error))
    await refreshRunning()
  }

  async function pauseOrResume() {
    setBusy(true)
    setErr(null)
    const { error } = await api.POST(paused ? '/time/resume' : '/time/pause', {
      params: { header: workspaceHeader() },
      body: { task_id: cur.task_id },
    })
    setBusy(false)
    if (error) setErr(errMessage(error))
    await refreshRunning()
  }

  return (
    <span className="running">
      <span
        className={paused ? 'running__spin is-paused' : 'running__spin'}
        aria-hidden="true"
      />
      {ordered.length > 1 ? (
        <button
          type="button"
          className="running__n"
          title={t('time.nextTimer')}
          aria-label={t('time.nextTimer')}
          onClick={() => setIdx((safeIdx + 1) % ordered.length)}
        >
          {safeIdx + 1}/{ordered.length}
        </button>
      ) : (
        <span className="running__n">1/1</span>
      )}
      <Link
        to={`/tasks/${cur.task_id}`}
        className="running__link"
        title={t('time.runningNow')}
        aria-label={t('time.runningNow')}
      >
        <span className="running__title">
          <span>{title}</span>
        </span>
        <span className={paused ? 'running__t is-paused' : 'running__t'}>
          {hms(activeElapsedSec(cur, now))}
        </span>
      </Link>
      <button
        type="button"
        className="running__ctl"
        disabled={busy}
        title={paused ? t('time.resume') : t('time.pause')}
        aria-label={paused ? t('time.resume') : t('time.pause')}
        onClick={() => void pauseOrResume()}
      >
        {paused ? '▶' : '⏸'}
      </button>
      <MemoPopover
        entry={cur}
        triggerClassName="running__ctl"
        onOpenChange={setEditing}
      />
      <button
        type="button"
        className="running__ctl"
        disabled={busy}
        title={t('time.stop')}
        aria-label={t('time.stop')}
        onClick={() => void stop()}
      >
        ■
      </button>
      {err && <span className="err">{err}</span>}
    </span>
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

// Single multi-state "acting as" chip (replaces the dropdown + the
// separate admin badge). Click cycles through the modes the user is
// entitled to: User -> [Owner] -> [Admin] -> User. Least-privilege
// (User) is the default; the server re-checks every elevation.
function ModeChip({
  canOwner,
  canAdmin,
  wsRole,
  adminOn,
}: {
  canOwner: boolean
  canAdmin: boolean
  wsRole: string
  adminOn: boolean
}) {
  const { t } = useTranslation()
  const modes = [
    'user',
    ...(canOwner ? ['owner'] : []),
    ...(canAdmin ? ['admin'] : []),
  ]
  if (modes.length < 2) return null
  const cur = adminOn ? 'admin' : wsRole === 'owner' ? 'owner' : 'user'
  const apply = (m: string) => {
    if (m === 'admin') {
      // Platform admin (god mode): the backend's effective_role
      // ceiling becomes ``owner`` for any workspace. Without sending
      // X-Workspace-Role=owner explicitly, the request would default
      // to ``member`` and clamp DOWN to member — admin-mode would
      // not actually unlock owner-gated routes (workflows save,
      // tag CRUD). Force the requested role to owner so admin-mode
      // really is "act as owner of this workspace".
      setAdminMode(true)
      setWorkspaceRole('owner')
    } else if (m === 'owner') {
      setAdminMode(false)
      setWorkspaceRole('owner')
    } else {
      setAdminMode(false)
      setWorkspaceRole('')
    }
  }
  const label =
    cur === 'admin'
      ? t('rolesw.admin')
      : cur === 'owner'
        ? t('roles.owner')
        : t('rolesw.user')
  return (
    <button
      type="button"
      className={`modechip modechip--${cur}`}
      title={t('rolesw.switchHint')}
      onClick={() => apply(modes[(modes.indexOf(cur) + 1) % modes.length])}
    >
      {cur === 'admin' && <Icon name="shield" />}
      {label}
    </button>
  )
}

export function AppShell() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const { me } = useMe()
  // Adopt the stored language once the profile loads, so the SPA opens in
  // the user's saved language (i18next itself does not persist) and the UI
  // matches the language their reminders are sent in.
  useEffect(() => {
    if (me?.language && me.language !== i18n.language) {
      void i18n.changeLanguage(me.language)
    }
  }, [me?.language])
  const elevated = useAdminMode()
  const canAdmin = !!me?.is_admin
  // Mobile sidebar toggle. The hamburger in topbar flips this; CSS
  // media query at <820px overlays the sidebar as a drawer when
  // ``.app--sidebar-open`` is set, otherwise it stays off-screen.
  // Desktop ignores both — the sidebar is always docked.
  const [sidebarOpen, setSidebarOpen] = useState(false)
  // Below the layout breakpoint the sidebar is an off-canvas drawer and
  // the topbar utilities (theme/lang/mode/settings/logout) relocate into
  // its foot. One JS source of truth, kept in sync with the CSS media
  // query (see useMediaQuery).
  const isMobile = useMediaQuery(MOBILE_QUERY)
  // Workspace role switcher: only meaningful if the entitlement
  // ceiling is above member (an owner/admin can act down as a user).
  const { ws } = useMyWorkspace()
  const wsRole = useWorkspaceRole()
  // Owner is the only privileged namespace role to switch into;
  // platform admin is a separate axis (handled by the same chip).
  const canSwitchRole = (ws?.my_role ?? 'member') === 'owner'

  // One capture-phase interceptor, for the one click target the browser
  // would otherwise mishandle: a UUID-prefix chip (the entityChips
  // decoration over the markdown source) carries a ``data-entity-prefix``
  // attribute. Resolve it (the cache is warmed by the editor's resolver
  // loop, so this is usually synchronous) and route to the entity, falling
  // back to the /t/:prefix resolver route (a friendly 404 / disambiguator)
  // when nothing is cached yet.
  //
  // It used to carry three more branches: one for mention links, two for
  // the shapes of attachment link. They were there because the
  // document-model editor put plain <a> marks in the DOM with no React
  // handler on them. That editor is gone and its replacement renders no
  // anchors at all, so every link a reader can click now comes from
  // MarkdownView, which turns its own mentions into router links and marks
  // its own attachment links `md-att` with a preventDefault on them. The
  // branches had nothing left to serve.
  //
  // What keeps that true is the invariant asserted in Markdown.test.tsx: an
  // attachment href reaching the DOM without `md-att` navigates in the clear
  // and answers 401, because the route is bearer-authenticated. If that test
  // fails, the fix is the renderer, or a fallback back here -- never a
  // relaxed assertion.
  useEffect(() => {
    const onClick = (e: MouseEvent) => {
      const target = e.target as HTMLElement | null
      const chip = target?.closest('[data-entity-prefix]') as HTMLElement | null
      const prefix = chip?.getAttribute('data-entity-prefix') ?? ''
      if (!prefix) return
      e.preventDefault()
      const go = (res: LookupOut | null) => {
        const m =
          res?.matches.find((x) => x.kind === 'task') ?? res?.matches?.[0]
        navigate(m ? m.route_url : `/t/${prefix}`)
      }
      const cached = getCachedLookup(prefix, RESOLVE_ID)
      if (cached) go(cached)
      else
        void lookupPrefix(prefix, RESOLVE_ID)
          .then(go)
          .catch(() => navigate(`/t/${prefix}`))
    }
    document.addEventListener('click', onClick, true)
    return () => document.removeEventListener('click', onClick, true)
  }, [navigate])

  // Drawer: close on Escape (keyboard parity with the backdrop tap).
  useEffect(() => {
    if (!sidebarOpen) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setSidebarOpen(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [sidebarOpen])

  // Lock body scroll while the mobile drawer overlays the page, so the
  // content underneath does not scroll behind the drawer.
  useEffect(() => {
    const lock = isMobile && sidebarOpen
    document.body.classList.toggle('body--locked', lock)
    return () => document.body.classList.remove('body--locked')
  }, [isMobile, sidebarOpen])

  async function onLogout() {
    // Real server-side logout: revoke the JWT *and* the refresh
    // family (if we have a refresh token), then drop the session.
    const rt = getSession()?.refreshToken
    await api.POST('/auth/logout', {
      body: rt ? { refresh_token: rt } : { refresh_token: null },
    })
    clearSession()
  }

  const groups: { title: string; items: Item[] }[] = [
    {
      title: t('nav.groups.knowledge'),
      items: [
        { to: '/notes', label: t('notes.nav'), icon: 'notes' },
        { to: '/garden', label: t('garden.nav'), icon: 'notes' },
        { to: '/memory', label: t('memory.nav'), icon: 'memory' },
      ],
    },
    {
      title: t('nav.groups.productivity'),
      items: [
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
  // Admin nav appears only while elevated (sudo-style): a normal-mode
  // admin sees exactly the normal app.
  if (canAdmin && elevated) {
    groups.push({
      title: t('nav.groups.admin'),
      items: [{ to: '/admin/users', label: t('admin.usersNav'), icon: 'shield' }],
    })
  }

  const closeDrawer = () => setSidebarOpen(false)

  // Secondary controls. On desktop they sit in the topbar; below the
  // layout breakpoint they relocate into the drawer foot (rendered in
  // exactly one place at a time, so no duplicate controls in the DOM).
  const utilities = (
    <>
      <UserAvatar />
      <ModeChip
        canOwner={canSwitchRole}
        canAdmin={canAdmin}
        wsRole={wsRole}
        adminOn={elevated}
      />
      <ThemeToggle />
      <select
        aria-label="language"
        value={i18n.language}
        onChange={(e) => {
          const lng = e.target.value
          void i18n.changeLanguage(lng)
          // Persist so the choice survives a reload AND so worker-generated
          // notifications (reminders) reach this user in their language.
          void api.PATCH('/auth/me', { body: { language: lng } })
        }}
      >
        <option value="en">EN</option>
        <option value="it">IT</option>
      </select>
      <NavLink
        to="/settings"
        onClick={closeDrawer}
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
    </>
  )

  return (
    <div className={'app' + (sidebarOpen ? ' app--sidebar-open' : '')}>
      <CommandPalette />
      {/* Mounted at the shell, not per-route: a new deploy is a property
          of the app, not of the page you happen to be on. Renders nothing
          in the ordinary case (the watcher reloads silently when nothing
          is unsaved) — see lib/useBuildWatch.ts. */}
      <UpdateBanner />
      <header className="topbar">
        <button
          type="button"
          className="topbar__hamburger"
          aria-label={t('nav.toggleSidebar')}
          aria-expanded={sidebarOpen}
          onClick={() => setSidebarOpen((v) => !v)}
        >
          ☰
        </button>
        <div className="topbar__brand">
          <Logo /> {t('app.title')}
        </div>
        <div className="topbar__actions">
          {!isMobile && <RunningIndicator />}
          <PomodoroTimer />
          {!isMobile && utilities}
        </div>
      </header>
      {/* Mobile: the running indicator lives below the topbar on its
          own sticky row so the topbar stays compact for the pomodoro.
          When no timer is running the component returns null and the
          wrapper collapses via :empty (CSS), occupying no space. */}
      {isMobile && (
        <div className="topbar__row2">
          <RunningIndicator />
        </div>
      )}
      {isMobile && sidebarOpen && (
        <div
          className="sidebar__backdrop"
          aria-hidden="true"
          onClick={closeDrawer}
        />
      )}
      <div className="layout">
        <aside className="sidebar">
          <div className="sidebar__ws">
            {/* Which workspace you are in comes FIRST: the focus below it
                (client/project) only means anything inside one, and a
                client id carried into another tenant names nothing. */}
            <WorkspaceSwitcher />
            <ProjectFocus />
          </div>
          <nav
            className="nav"
            onClick={(e) => {
              // Tapping a destination closes the drawer on mobile.
              if (isMobile && (e.target as HTMLElement).closest('a'))
                closeDrawer()
            }}
          >
            {groups.map((g) => (
              <NavGroup key={g.title} title={g.title} items={g.items} />
            ))}
          </nav>
          {isMobile && <div className="nav__util">{utilities}</div>}
        </aside>
        <main className="content">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
