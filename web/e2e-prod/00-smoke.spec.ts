import { test, expect } from '@playwright/test'

test('storageState auth lands on app, not on /login', async ({ page }) => {
  await page.goto('/')
  await expect(page).not.toHaveURL(/\/login/)
  // Some authenticated landing element. The header always renders the
  // workspace name "Personal" for the test user.
  await expect(page.locator('body')).toContainText(/Personal/i, { timeout: 15_000 })
})
