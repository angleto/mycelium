import { test, expect, type Page } from '@playwright/test'
import { E2E_EMAIL as EMAIL, E2E_PASSWORD as PASSWORD } from './global-setup'

// ADR-0035 garden health dashboard: the /garden/health page renders one
// card per sensor, with floors and "show, never judge" empty readings,
// and the garden page links to it.

async function login(page: Page) {
  await page.goto('/login')
  await page.locator('input[type=email]').fill(EMAIL)
  await page.locator('input[type=password]').fill(PASSWORD)
  await page.locator('button[type=submit]').click()
  await page.waitForURL('**/notes', { timeout: 15_000 })
}

test('health dashboard renders the live sensor cards + a not-yet-measured section', async ({
  page,
}) => {
  await login(page)
  await page.goto('/garden/health')

  await expect(page.locator('.ghealth h1')).toBeVisible()
  // The eight live sensors render as cards (incl. embedding_coverage, WS-A,
  // and recall_at_k once search-click logging unblocked it); the remaining
  // not-yet-wired one lives in its own section, not as an empty card.
  await expect(page.locator('.ghealth__grid .ghealth__card')).toHaveCount(8)

  // Headline sensor declares its health floor, formatted as a percentage
  // (not a raw 0.40 float).
  const accept = page.locator('.ghealth__card', { hasText: 'Accept rate (7d)' })
  await expect(accept.locator('.ghealth__floor')).toContainText('40%')

  // A sensor still flagged not-yet-wired is listed apart with its reason,
  // never a faked number ("show, never judge").
  const pending = page.locator('.ghealth__pending')
  await expect(pending).toContainText('Fungal lag')
  await expect(pending.locator('.ghealth__pending-reason').first()).toBeVisible()
})

test('clicking a sensor opens the per-metric drill-down', async ({ page }) => {
  await login(page)
  await page.goto('/garden/health')

  const accept = page.locator('.ghealth__card', { hasText: 'Accept rate (7d)' })
  await accept.click()

  const drill = page.locator('.ghealth__drill')
  await expect(drill).toBeVisible()
  await expect(drill).toContainText('Accept rate (7d)')
  // Fresh workspace: no daily snapshots yet -> the graceful "not enough
  // history" state, never an error or a faked line.
  await expect(drill.locator('.ghealth__drill-empty')).toBeVisible()

  await drill.locator('.ghealth__drill-close').click()
  await expect(drill).toBeHidden()
})

test('health dashboard renders the "what changed" timeline below the cards', async ({ page }) => {
  await login(page)
  await page.goto('/garden/health')

  // The timeline section renders below the cards (ADR-0035 §84) with its
  // heading; it shows either the empty state or a list of factual events,
  // never an error. Robust to whatever history the shared account carries.
  const timeline = page.locator('.ghealth__timeline')
  await expect(timeline).toBeVisible()
  await expect(timeline.locator('.ghealth__timeline-head')).toContainText('What changed')
  await expect(
    timeline.locator('.ghealth__timeline-empty, .ghealth__timeline-list'),
  ).toBeVisible()
  await expect(timeline.locator('.error')).toHaveCount(0)
})

test('garden page links to the health dashboard', async ({ page }) => {
  await login(page)
  await page.goto('/garden')
  await page.getByRole('link', { name: /Garden health/ }).click()
  await expect(page).toHaveURL(/\/garden\/health$/)
  await expect(page.locator('.ghealth h1')).toBeVisible()
})
