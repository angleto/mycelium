import { test, expect, type Page } from '@playwright/test'

const EMAIL = 'angelo@leto.blue'
const PASSWORD = 'testp4ssw0rd'

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

  // doCreate keeps the modal open in edit mode on the new note: the
  // reusable TagPicker is present and the note already carries its
  // (Personal) client tag (every note belongs to a client).
  await expect(dialog.locator('.tagpick')).toBeVisible()
  await expect(dialog.locator('.tagpick__search')).toBeVisible()
  await expect(dialog.locator('.tagpick .chip--rm').first()).toBeVisible()

  // Convert to task -> a link to the created task appears.
  await dialog
    .getByRole('button', { name: /convert to task|trasforma in task/i })
    .click()
  await expect(
    page.getByText(/task created from note|task creato dalla nota/i),
  ).toBeVisible()
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
  const chip = page.locator('.modechip')
  await chip.click()
  await expect(chip).toHaveClass(/modechip--owner/)
  await page.goto('/clients')

  // Unique names per run: client/project tag names are unique per org,
  // so reused fixed names would (correctly) be rejected on re-runs.
  const stamp = Date.now()
  const cname = `E2E client ${stamp}`
  const pname = `E2E project ${stamp}`
  const form = page.locator('form.cpform').first()
  await form.locator('input[name=name]').fill(cname)
  await form.locator('input[name=ragione_sociale]').fill(`${cname} SRL`)
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
  // hint) — never the old indistinct chip/chip--rm pair.
  await expect(page.locator('.tagfilter')).toBeVisible()
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
  await page.goto('/clients')
  const form = page.locator('form.cpform').first()
  const stamp = Date.now()
  await form.locator('input[name=name]').fill(`RBAC ${stamp}`)
  await form.locator('input[name=ragione_sociale]').fill(`RBAC ${stamp} srl`)
  await form.getByRole('button', { name: /^add$|^aggiungi$/i }).click()
  await expect(page.locator('main.content .err')).toBeVisible()

  // Switch to Owner via the chip, retry → succeeds.
  const chip = page.locator('.modechip')
  await chip.click()
  await expect(chip).toHaveClass(/modechip--owner/)
  await page.goto('/clients')
  const form2 = page.locator('form.cpform').first()
  await form2.locator('input[name=name]').fill(`RBAC ${stamp}`)
  await form2
    .locator('input[name=ragione_sociale]')
    .fill(`RBAC ${stamp} srl`)
  await form2.getByRole('button', { name: /^add$|^aggiungi$/i }).click()
  await expect(
    page.locator('.cpitem', { hasText: `RBAC ${stamp}` }).first(),
  ).toBeVisible()
  await expect(page.locator('main.content .err')).toHaveCount(0)
  expect(errors, errors.join('\n')).toEqual([])
})
