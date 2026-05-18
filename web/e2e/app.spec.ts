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
  await page.goto('/clients')

  const cname = `E2E client ${Date.now()}`
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
  await add.locator('input').fill('E2E project')
  await add.getByRole('button', { name: /^add$|^aggiungi$/i }).click()
  await expect(item.getByText('E2E project').first()).toBeVisible()
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

test('admin: sudo elevation reveals user administration', async ({
  page,
}) => {
  const errors: string[] = []
  watch(page, errors)
  await login(page)

  // Not elevated by default: a ghost "Admin" entry control, no badge.
  const enter = page.locator('.topbar__admin')
  await expect(enter).toBeVisible()
  await expect(page.locator('.badge--admin')).toHaveCount(0)

  await enter.click()
  // Elevated: badge appears, admin nav appears.
  await expect(page.locator('.badge--admin')).toBeVisible()
  await page.goto('/admin/users')
  const row = page.locator('table.tbl tbody tr', { hasText: EMAIL }).first()
  await expect(row).toBeVisible()
  await expect(row).toContainText(/admin/i)

  // Exit elevation.
  await page.locator('.badge--admin').click()
  await expect(page.locator('.topbar__admin')).toBeVisible()
  expect(errors, errors.join('\n')).toEqual([])
})
