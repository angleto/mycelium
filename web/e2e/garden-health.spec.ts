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

test('health dashboard renders the seven sensor cards', async ({ page }) => {
  await login(page)
  await page.goto('/garden/health')

  await expect(page.locator('.ghealth h1')).toBeVisible()
  await expect(page.locator('.ghealth__card')).toHaveCount(8)

  // Headline sensor declares its health floor (always shown).
  const accept = page.locator('.ghealth__card', { hasText: 'Accept rate (7d)' })
  await expect(accept.locator('.ghealth__floor')).toContainText('0.4')

  // A permanently-blocked sensor shows an explicit empty reading, never
  // a faked number ("show, never judge").
  const fungal = page.locator('.ghealth__card', { hasText: 'Fungal lag' })
  await expect(fungal.locator('.ghealth__value--empty')).toHaveText('No reading yet')
})

test('garden page links to the health dashboard', async ({ page }) => {
  await login(page)
  await page.goto('/garden')
  await page.getByRole('link', { name: /Garden health/ }).click()
  await expect(page).toHaveURL(/\/garden\/health$/)
  await expect(page.locator('.ghealth h1')).toBeVisible()
})
