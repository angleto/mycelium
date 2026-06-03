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
  // The six wired sensors render as cards; the two unwired ones live in
  // their own section, not as permanently-empty cards.
  await expect(page.locator('.ghealth__grid .ghealth__card')).toHaveCount(6)

  // Headline sensor declares its health floor, formatted as a percentage
  // (not a raw 0.40 float).
  const accept = page.locator('.ghealth__card', { hasText: 'Accept rate (7d)' })
  await expect(accept.locator('.ghealth__floor')).toContainText('40%')

  // A permanently-unwired sensor is listed apart with its blocker reason,
  // never a faked number ("show, never judge").
  const pending = page.locator('.ghealth__pending')
  await expect(pending).toContainText('Fungal lag')
  await expect(pending.locator('.ghealth__pending-reason').first()).toBeVisible()
})

test('garden page links to the health dashboard', async ({ page }) => {
  await login(page)
  await page.goto('/garden')
  await page.getByRole('link', { name: /Garden health/ }).click()
  await expect(page).toHaveURL(/\/garden\/health$/)
  await expect(page.locator('.ghealth h1')).toBeVisible()
})
