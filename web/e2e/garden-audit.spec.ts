import { test, expect, type Page } from '@playwright/test'
import { E2E_EMAIL as EMAIL, E2E_PASSWORD as PASSWORD } from './global-setup'

// ADR-0036 audit panel: /garden/audit renders the coordinated event
// stream (read/propose/commit/reject), and the garden page links to it.

async function login(page: Page) {
  await page.goto('/login')
  await page.locator('input[type=email]').fill(EMAIL)
  await page.locator('input[type=password]').fill(PASSWORD)
  await page.locator('button[type=submit]').click()
  await page.waitForURL('**/notes', { timeout: 15_000 })
}

test('audit panel renders the event stream (empty state or list, never error)', async ({
  page,
}) => {
  await login(page)
  await page.goto('/garden/audit')

  await expect(page.locator('.ghaudit h1')).toBeVisible()
  // Robust to whatever history the shared account carries: either the
  // empty state or a list of events, but never the load-error state.
  await expect(page.locator('.ghaudit__empty, .ghaudit__list')).toBeVisible()
  await expect(page.locator('.ghaudit .error')).toHaveCount(0)
})

test('garden page links to the audit panel', async ({ page }) => {
  await login(page)
  await page.goto('/garden')
  await page.getByRole('link', { name: /Event stream/ }).click()
  await expect(page).toHaveURL(/\/garden\/audit$/)
  await expect(page.locator('.ghaudit h1')).toBeVisible()
})
