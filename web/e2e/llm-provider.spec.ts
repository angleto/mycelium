import { test, expect, type Page } from '@playwright/test'
import { E2E_EMAIL as EMAIL, E2E_PASSWORD as PASSWORD } from './global-setup'

// Per-org LLM provider selection (task d2c60a83). The admin-gated
// LlmProviderSettings card in /settings lets a workspace admin pick a
// hosted provider (here Scaleway) on the platform key (our_key) and have
// it persist. BYOK + the fail-closed key probe hit the backend->provider
// network and are covered by backend respx tests; this e2e stays offline:
// with no key configured the curated roster endpoint returns the static
// list (no /v1/models call) and our_key save does not probe.

async function loginAsAdmin(page: Page) {
  await page.goto('/login')
  await page.locator('input[type=email]').fill(EMAIL)
  await page.locator('input[type=password]').fill(PASSWORD)
  await page.locator('button[type=submit]').click()
  await page.waitForURL('**/notes', { timeout: 15_000 })
  // Elevate to admin mode (the card is gated on is_admin && adminMode) and
  // assert the owner workspace role (the PUT is admin-gated; the backend
  // clamps X-Workspace-Role to actual membership, so this only works for a
  // real owner, which the bootstrap admin is). Navigating fresh after makes
  // isAdminMode() / getWorkspaceRole() pick the flags up.
  await page.evaluate(() => {
    localStorage.setItem('mycelium.adminMode', '1')
    localStorage.setItem('mycelium.workspaceRole', 'owner')
  })
}

test('admin selects Scaleway on the platform key and it persists', async ({
  page,
}) => {
  await loginAsAdmin(page)
  await page.goto('/settings')

  const card = page.locator('section.card', { hasText: 'AI model provider' })
  await expect(card.locator('h2')).toHaveText('AI model provider')

  // Pick Scaleway -> the curated roster loads into the model datalist.
  await card.locator('select').first().selectOption('scaleway')
  const model = card.locator('input[list="scw-models"]')
  await expect(model).toBeVisible()
  await model.fill('gpt-oss-120b')

  // Default key mode is the platform key (our_key) for a fresh org; save.
  await card.getByRole('button', { name: 'Save', exact: true }).click()
  await expect(card.locator('.ok')).toContainText('Saved')

  // Reload: the selection persisted (provider + model round-trip).
  await page.goto('/settings')
  const card2 = page.locator('section.card', { hasText: 'AI model provider' })
  await expect(card2.locator('select').first()).toHaveValue('scaleway')
  await expect(card2.locator('input[list="scw-models"]')).toHaveValue(
    'gpt-oss-120b',
  )
})

test('switching to BYOK requires a key before saving', async ({ page }) => {
  await loginAsAdmin(page)
  await page.goto('/settings')

  const card = page.locator('section.card', { hasText: 'AI model provider' })
  await card.locator('select').first().selectOption('scaleway')
  // Choose "use my own key" but leave the key empty -> save is blocked
  // client-side with a hint (no network, no probe).
  await card.getByText('Use my own key (BYOK)').click()
  await card.getByRole('button', { name: 'Save', exact: true }).click()
  await expect(card.locator('.err')).toBeVisible()
})
