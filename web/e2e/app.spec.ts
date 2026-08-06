import { test, expect, type Page } from '@playwright/test'
// Single source of truth: the neutral admin the globalSetup bootstraps
// (no personal data in the repo; decoupled from external seeding).
import { E2E_EMAIL as EMAIL, E2E_PASSWORD as PASSWORD } from './global-setup'

// Console / uncaught-error capture. Uncaught exceptions (pageerror)
// are always a real bug. console.error is collected minus known
// framework/network noise.
const BENIGN = [
  /favicon/i,
  /React DevTools/i,
  /ResizeObserver loop/i,
  /\[vite\]/i,
  /Download the React DevTools/i,
  // A handled 4xx (auth / RBAC denial / validation / optimistic
  // concurrency) makes the browser auto-log "Failed to load
  // resource"; the app surfaces it via the .err banner. Not a bug.
  // 5xx is NOT filtered — a server crash must still fail the gate.
  /Failed to load resource: the server responded with a status of 4\d\d/i,
]

function watch(page: Page, errors: string[]) {
  page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`))
  page.on('console', (m) => {
    if (m.type() !== 'error') return
    const t = m.text()
    if (BENIGN.some((re) => re.test(t))) return
    errors.push(`console.error: ${t}`)
  })
}

async function login(page: Page) {
  await page.goto('/login')
  await page.locator('input[type=email]').fill(EMAIL)
  await page.locator('input[type=password]').fill(PASSWORD)
  await page.locator('button[type=submit]').click()
  await page.waitForURL('**/notes', { timeout: 15_000 })
}

// Drive the mode chip to a target mode deterministically. The chip
// cycles user → owner → admin → user, but it only *offers* a mode once
// the entitlement query backing it has resolved: `owner` needs
// GET /workspaces/me (ws.my_role === 'owner') and `admin` needs
// GET /auth/me (is_admin), fetched independently after login. A single
// blind cycle can therefore miss `owner` when it clicks before
// /workspaces/me lands — harmless locally (warm queries), but slow
// enough on cold-Postgres CI to leave the chip stuck cycling
// user↔admin, which is exactly the app.spec ensureMode flake. Retry the
// whole cycle until the chip actually reaches the target: robust to
// both the incoming state and the data-load race.
async function ensureMode(page: Page, target: 'user' | 'owner' | 'admin') {
  const chip = page.locator('.modechip')
  await expect(chip).toBeVisible()
  await expect(async () => {
    const at = await chip.evaluate(
      (el, t) => el.classList.contains(`modechip--${t}`),
      target,
    )
    if (at) return
    await chip.click()
    throw new Error(`mode chip not at ${target} yet`)
  }).toPass({ timeout: 10_000 })
}

const ROUTES = [
  '/notes',
  '/tasks',
  '/trash',
  '/clients',
  '/workflows',
  '/graph',
  '/schedule',
  '/calendar',
  '/time',
  '/advisory',
  '/budgets',
  '/email',
  '/billing',
  '/memory',
  '/invoices',
  '/notifications',
  '/tags',
  '/workspace',
  '/settings',
]

test.describe.configure({ mode: 'serial' })

test('login centers and authenticates', async ({ page }) => {
  const errors: string[] = []
  watch(page, errors)
  await page.goto('/login')
  // The auth card must be centered (not pinned top-left): its left
  // edge is well away from x=0.
  const card = page.locator('form.card')
  await expect(card).toBeVisible()
  const box = await card.boundingBox()
  expect(box).not.toBeNull()
  expect(box!.x).toBeGreaterThan(60)
  await login(page)
  expect(errors, errors.join('\n')).toEqual([])
})

test('every route renders without crashing or console errors', async ({
  page,
}) => {
  const errors: string[] = []
  watch(page, errors)
  await login(page)
  for (const r of ROUTES) {
    await page.goto(r)
    // Still authenticated (not bounced to /login) and the app shell +
    // a heading rendered.
    await expect(page, `route ${r} lost auth`).not.toHaveURL(/\/login/)
    await expect(
      page.locator('main.content'),
      `route ${r} no content`,
    ).toBeVisible()
    await expect(
      page.locator('main.content :is(h1,h2)').first(),
      `route ${r} no heading`,
    ).toBeVisible()
  }
  expect(errors, errors.join('\n')).toEqual([])
})

test('notes: create, client auto-assigned, convert to task', async ({
  page,
}) => {
  const errors: string[] = []
  watch(page, errors)
  await login(page)
  await page.goto('/notes')

  const title = `E2E note ${Date.now()}`
  await page.getByRole('button', { name: /new note|nuova nota/i }).click()
  const dialog = page.locator('.modal__panel')
  await expect(dialog).toBeVisible()
  await dialog.locator('input').first().fill(title)
  await dialog.locator('.ProseMirror').first().click()
  await page.keyboard.type('A note created by the E2E suite.')
  await dialog
    .getByRole('button', { name: /create note|crea nota/i })
    .click()

  // doCreate navigates to the full note page in edit mode: the reusable
  // TagPicker is present in the Details rail and the note already carries
  // its (Personal) client tag (every note belongs to a client).
  await page.waitForURL(/\/notes\/[0-9a-f-]+/, { timeout: 15_000 })
  await expect(page.locator('.tagpick')).toBeVisible()
  await expect(page.locator('.tagpick__search')).toBeVisible()
  // The client is STRUCTURAL since ADR-0050: a note has exactly one and it
  // cannot be detached, so it renders as a <select> rather than a chip with
  // an ✕. ``.chip--rm`` now covers only the free-form tags, of which a
  // freshly created note has none -- asserting on it here checked that the
  // client was *removable*, which is precisely what the invariant forbids.
  const clientSelect = page
    .locator('.tagpick label')
    .filter({ hasText: /client|cliente/i })
    .locator('select')
  await expect(clientSelect).toBeVisible()
  await expect(clientSelect).not.toHaveValue('')

  // Derive a task (ADR-0029): the button is "Derive task" / "Genera task"
  // and, on success, the app navigates straight to the freshly-derived
  // task, whose title is inherited from the note.
  await page
    .getByRole('button', { name: /derive task|genera task/i })
    .click()
  await page.waitForURL(/\/tasks\/[0-9a-f-]{8}/, { timeout: 15_000 })
  await expect(
    page.getByRole('textbox', { name: /new task title|titolo nuovo task/i }),
  ).toHaveValue(title)
  expect(errors, errors.join('\n')).toEqual([])
})

test('clients: create a client and add a project inline', async ({
  page,
}) => {
  const errors: string[] = []
  watch(page, errors)
  await login(page)
  // Creating clients/projects is an owner-only privileged write under
  // the hardened model: elevate via the mode chip first (what an
  // owner does in the real UI).
  await ensureMode(page, 'owner')
  await page.goto('/clients')

  // Unique names per run: client/project tag names are unique per org,
  // so reused fixed names would (correctly) be rejected on re-runs.
  const stamp = Date.now()
  const cname = `E2E client ${stamp}`
  const pname = `E2E project ${stamp}`
  const form = page.locator('form.cpform').first()
  await form.locator('input[name=name]').fill(cname)
  await form.locator('input[name=legal_name]').fill(`${cname} SRL`)
  await form.getByRole('button', { name: /^add$|^aggiungi$/i }).click()

  const item = page.locator('.cpitem', { hasText: cname }).first()
  await expect(item).toBeVisible()
  // Expand and add a project under this client.
  await item.locator('.cpcaret').click()
  const add = item.locator('form.cpadd')
  await expect(add).toBeVisible()
  await add.locator('input').fill(pname)
  await add.getByRole('button', { name: /^add$|^aggiungi$/i }).click()
  await expect(item.getByText(pname).first()).toBeVisible()
  // No in-app error banner (backend rejections don't hit the console).
  await expect(page.locator('main.content .err')).toHaveCount(0)
  expect(errors, errors.join('\n')).toEqual([])
})

test('graph: catalog tag filter + proper scope label', async ({ page }) => {
  const errors: string[] = []
  watch(page, errors)
  await login(page)
  await page.goto('/graph')
  // The scope field is not mislabelled "All".
  const scopeLabel = page.locator('main.content label', {
    has: page.locator('select'),
  }).first()
  await expect(scopeLabel).not.toHaveText(/^All$|^Tutti$/)
  // Tag filter region present (chips from the catalog, or the empty
  // hint) — never the old indistinct chip/chip--rm pair. GraphRoute
  // renders two .tagfilter blocks (state filter + tag filter); the tag
  // one is rendered last.
  await expect(page.locator('.tagfilter').last()).toBeVisible()
  expect(errors, errors.join('\n')).toEqual([])
})

test('mode chip cycles User → Owner → Admin and gates accordingly', async ({
  page,
}) => {
  const errors: string[] = []
  watch(page, errors)
  await login(page) // angelo: owner of Personal AND platform admin

  const chip = page.locator('.modechip')
  await expect(chip).toBeVisible()
  // Least privilege by default.
  await expect(chip).toHaveClass(/modechip--user/)

  // Workspace page: the owner is listed as a member, but in user mode
  // the management controls are gated. The rename field is purely
  // canManage-gated (the Add-member button also depends on the email
  // field, so it is not a clean signal).
  await page.goto('/workspace')
  await expect(
    page.locator('table.tbl tbody tr', { hasText: EMAIL }).first(),
  ).toBeVisible()
  const rename = page
    .locator('.card input:not([readonly])')
    .first()
  await expect(rename).toBeDisabled()

  // Cycle → Owner: management enabled.
  await chip.click()
  await expect(chip).toHaveClass(/modechip--owner/)
  await expect(rename).toBeEnabled()

  // Cycle → Admin: platform admin surface reachable.
  await chip.click()
  await expect(chip).toHaveClass(/modechip--admin/)
  await page.goto('/admin/users')
  // The list is one bounded page, newest first: find the account by
  // SEARCH rather than expecting it on the first page. On a database with
  // real history this row is nowhere near the top, which is the whole
  // reason the endpoint was paginated.
  await page.locator('input.adminsearch').fill(EMAIL)
  const arow = page
    .locator('table.tbl tbody tr', { hasText: EMAIL })
    .first()
  await expect(arow).toBeVisible()
  await expect(arow).toContainText(/admin/i)

  // Cycle → back to User.
  await page.locator('.modechip').click()
  await expect(page.locator('.modechip')).toHaveClass(/modechip--user/)
  expect(errors, errors.join('\n')).toEqual([])
})

test('effective role: privileged write needs Owner mode', async ({
  page,
}) => {
  const errors: string[] = []
  watch(page, errors)
  await login(page)

  // Default User mode: creating a client is rejected (owner-only),
  // surfaced as an in-app error, not a crash.
  await ensureMode(page, 'user')
  await page.goto('/clients')
  const form = page.locator('form.cpform').first()
  const stamp = Date.now()
  await form.locator('input[name=name]').fill(`RBAC ${stamp}`)
  await form.locator('input[name=legal_name]').fill(`RBAC ${stamp} srl`)
  await form.getByRole('button', { name: /^add$|^aggiungi$/i }).click()
  await expect(page.locator('main.content .err')).toBeVisible()

  // Switch to Owner via the chip, retry → succeeds.
  await ensureMode(page, 'owner')
  await page.goto('/clients')
  const form2 = page.locator('form.cpform').first()
  await form2.locator('input[name=name]').fill(`RBAC ${stamp}`)
  await form2
    .locator('input[name=legal_name]')
    .fill(`RBAC ${stamp} srl`)
  await form2.getByRole('button', { name: /^add$|^aggiungi$/i }).click()
  await expect(
    page.locator('.cpitem', { hasText: `RBAC ${stamp}` }).first(),
  ).toBeVisible()
  await expect(page.locator('main.content .err')).toHaveCount(0)
  expect(errors, errors.join('\n')).toEqual([])
})

test('task work note: open from task + billable timer in the note', async ({
  page,
}) => {
  const errors: string[] = []
  watch(page, errors)
  await login(page)

  // Make a task via the note→task convert (member-level).
  await page.goto('/notes')
  const title = `WN ${Date.now()}`
  await page.getByRole('button', { name: /new note|nuova nota/i }).click()
  const dialog = page.locator('.modal__panel')
  await dialog.locator('input').first().fill(title)
  await dialog.locator('.ProseMirror').first().click()
  await page.keyboard.type('seed')
  await dialog
    .getByRole('button', { name: /create note|crea nota/i })
    .click()
  // Create navigates to the note page; derive the task from there. Scope
  // to the detail pane (.notedetail): on the master-detail /notes layout the
  // still-visible list item for this note also carries a "Derive task"
  // button (notes.derive), so an unscoped role query is strict-mode-ambiguous.
  await page.waitForURL(/\/notes\/[0-9a-f-]+/, { timeout: 15_000 })
  await page
    .locator('.notedetail')
    .getByRole('button', { name: /derive task|genera task/i })
    .click()
  // Deriving (ADR-0029) navigates straight to the new task; capture its
  // path for the later re-visits.
  await page.waitForURL(/\/tasks\/[0-9a-f-]+/, { timeout: 15_000 })
  const href = new URL(page.url()).pathname

  // New work note (Proposal A: a note is the work log of the task) →
  // opens the linked note with the billable timer. The work-note actions
  // live under the task's "Notes" connection tab (role=tab), so activate it
  // first — it is not the default-selected tab.
  await page.getByRole('tab', { name: /^(notes|note)$/i }).click()
  await page
    .getByRole('button', { name: /new work note|nuova nota di lavoro/i })
    .click()
  await expect(page).toHaveURL(/\/notes\/[0-9a-f-]+/)
  const banner = page.locator('.notebanner')
  await expect(banner).toBeVisible()
  const timer = banner.locator('.tasktimer')
  await expect(timer).toBeVisible()
  // Start the timer (serial ▶, not parallel ▶▶) → running stop button.
  await timer.getByRole('button', { name: /^start$|^avvia$/i }).click()
  await expect(timer.locator('.tasktimer__stop')).toBeVisible()
  // Robustness (Toggl-like): a running timer is server state, not a
  // JS counter. Reloading drops every client-side timer; the top-bar
  // running indicator must come back from /time/running with its
  // elapsed derived from the server started_at — proving it survives
  // lid-close / reconnect / a fresh JS context.
  await page.reload()
  const chip = page.locator('.running')
  await expect(chip).toBeVisible()
  await expect(chip).toContainText(/\d:\d\d/)
  // Stop from the task's own timer; duration is computed server-side.
  await page.goto(href ?? '/tasks')
  const tdTimer = page.locator('.tasktimer').first()
  await expect(tdTimer.locator('.tasktimer__stop')).toBeVisible()
  await tdTimer.getByRole('button', { name: /^stop$|^ferma$/i }).click()
  // Server-truth signal: the shared running source reflects the stop,
  // so the top-bar running indicator disappears (robust across the
  // note-editor vs task-detail timer instances + the reload — a
  // per-instance button re-render races in the serial suite).
  await expect(page.locator('.running')).toHaveCount(0)
  await expect(page.locator('main.content .err')).toHaveCount(0)
  expect(errors, errors.join('\n')).toEqual([])
})

test('tasks: recent widget — newest first, configurable N, toggle persists', async ({
  page,
}) => {
  const errors: string[] = []
  watch(page, errors)
  await login(page)
  await page.goto('/tasks')

  // Quick-add three tasks (member-level write). The client picker is
  // sourced from /clients (which ensures the Personal default), so the
  // only required select auto-selects without racing the /tags load.
  // Wait for it before the first create so each task carries its client
  // tag.
  const form = page.locator('form.quickadd')
  await expect(form.locator('select[required]')).not.toHaveValue('')

  const stamp = Date.now()
  const titles = [`Recent A ${stamp}`, `Recent B ${stamp}`, `Recent C ${stamp}`]
  for (const tt of titles) {
    await form.locator('.quickadd__title').fill(tt)
    await form.locator('button[type=submit]').click()
    // The list reloads on create; the new task lands in the widget.
    await expect(
      page.locator('.recentlist .recentrow__title', { hasText: tt }),
    ).toBeVisible()
  }

  const widget = page.locator('.recentwidget')
  await expect(widget).toBeVisible()
  // Most recently created (C) sits on top; A and B are within the
  // default window (N=4), older suite tasks fill any remaining slots.
  await expect(widget.locator('.recentrow__title').first()).toContainText(
    titles[2],
  )
  for (const tt of titles) {
    await expect(
      widget.locator('.recentrow__title', { hasText: tt }),
    ).toBeVisible()
  }

  // Configurable N: shrink to 1 → only the newest survives.
  await widget.locator('.recentwidget__count input').fill('1')
  await expect(widget.locator('.recentrow')).toHaveCount(1)
  await expect(widget.locator('.recentrow__title').first()).toContainText(
    titles[2],
  )

  // Collapse → the list unmounts; the state must survive a full reload
  // (persisted in localStorage, like the view toggle).
  await widget.locator('.recentwidget__toggle').click()
  await expect(page.locator('.recentlist')).toHaveCount(0)
  await page.reload()
  await expect(page.locator('.recentwidget')).toBeVisible()
  await expect(page.locator('.recentlist')).toHaveCount(0)

  // Re-expand → the persisted N=1 is still in effect.
  await page.locator('.recentwidget__toggle').click()
  await expect(page.locator('.recentlist .recentrow')).toHaveCount(1)

  await expect(page.locator('main.content .err')).toHaveCount(0)
  expect(errors, errors.join('\n')).toEqual([])
})

test('memory: write a snippet and recall it (keyword fallback works)', async ({
  page,
}) => {
  const errors: string[] = []
  watch(page, errors)
  await login(page)
  await page.goto('/memory')

  const token = `zqx${Date.now()}`
  const card = page.locator('section.card').first()
  const ta = card.locator('textarea').first()
  await ta.fill(`Remember the ${token} fact.`)
  await card.getByRole('button', { name: /^write$|^salva$|^scrivi$/i }).click()
  // Reliable success signal: onWrite clears the textarea only on a
  // successful (free) write. ('.ok' also matches the always-present
  // "semantic on" hint, so it can't gate the write completing.)
  // Generous timeout: the first write loads the embedding model
  // (cold start), which can exceed the default expect timeout.
  await expect(ta).toHaveValue('', { timeout: 30_000 })

  // Recall it by its unique word (semantic when an embedder is
  // available, else graceful keyword/full-text fallback).
  const search = page.locator('section.card').nth(1)
  await search.locator('input').first().fill(token)
  await search.getByRole('button', { name: /^search$|^cerca$/i }).click()
  await expect(
    search.locator('ul.list li', { hasText: token }).first(),
  ).toBeVisible({ timeout: 30_000 })
  await expect(page.locator('main.content .err')).toHaveCount(0)
  expect(errors, errors.join('\n')).toEqual([])
})
