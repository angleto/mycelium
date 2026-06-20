import { test, expect, type Page } from '@playwright/test'
import { E2E_EMAIL as EMAIL, E2E_PASSWORD as PASSWORD } from './global-setup'

// ADR-0037 learning signals (task 8aff04b9): the /garden/health page renders
// a learning panel with the user's own reject-hotspots + 30-day prior drift.
// Read-only, "show, never judge"; on a fresh account both sides render their
// graceful empty state, never an error.

async function login(page: Page) {
  await page.goto('/login')
  await page.locator('input[type=email]').fill(EMAIL)
  await page.locator('input[type=password]').fill(PASSWORD)
  await page.locator('button[type=submit]').click()
  await page.waitForURL('**/notes', { timeout: 15_000 })
}

test('learning panel renders below the timeline with reject-hotspot + drift sections', async ({
  page,
}) => {
  await login(page)
  await page.goto('/garden/health')

  const learning = page.locator('.ghealth__learning')
  await expect(learning).toBeVisible()
  await expect(learning.locator('.ghealth__learning-head')).toContainText('Learning signals')

  // Both sub-panels render; on a shared/fresh account they show either data
  // or the empty state, never an error ("show, never judge").
  const hotspots = page.locator('.ghealth__hotspots')
  await expect(hotspots).toContainText('Most-declined suggestions')
  await expect(
    hotspots.locator('.ghealth__hotspot-list, .ghealth__learning-empty'),
  ).toBeVisible()

  const drift = page.locator('.ghealth__drift')
  await expect(drift).toContainText('Biggest prior shifts')
  await expect(drift.locator('.ghealth__drift-list, .ghealth__learning-empty')).toBeVisible()

  await expect(learning.locator('.error')).toHaveCount(0)
})
