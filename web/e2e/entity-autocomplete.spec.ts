import { test, expect, type Page } from '@playwright/test'
import { E2E_EMAIL as EMAIL, E2E_PASSWORD as PASSWORD } from './global-setup'
import { readSource } from './source-editor'

// ADR-0038 layer E (task 6e1b5b95): typing ``[[`` in the note editor
// opens an entity autocomplete; selecting a match inserts the 8-char
// backtick-prefix code span (the roadmap convention the EntityPrefix
// decoration renders as a live chip).

async function login(page: Page) {
  await page.goto('/login')
  await page.locator('input[type=email]').fill(EMAIL)
  await page.locator('input[type=password]').fill(PASSWORD)
  await page.locator('button[type=submit]').click()
  await page.waitForURL('**/notes', { timeout: 15_000 })
}

async function openFreshNoteEditor(page: Page, title: string) {
  await page.getByRole('button', { name: 'New note' }).click()
  await page.locator('.modal__panel input').first().fill(title)
  await page.locator('.modal__foot button:not(.btn--ghost)').first().click()
  // The note page opens with the multi-part editor.
  await expect(page.locator('.parts-editor')).toBeVisible({ timeout: 10_000 })
  await page.waitForTimeout(500)
}

test('[[ autocomplete inserts a backtick-prefix code span', async ({ page }) => {
  await login(page)
  // A note with a unique title so the typeahead has exactly one match.
  const marker = `e2e-autocomplete-${Date.now()}`
  await openFreshNoteEditor(page, marker)

  // New notes start with zero parts; add one to get a rich-text body.
  await page.getByRole('button', { name: 'Add part' }).click()
  const editor = page.locator('.cm-content').first()
  await expect(editor).toBeVisible({ timeout: 10_000 })
  await editor.click()
  // Trigger + the full unique title (no spaces -> single suggestion query).
  await page.keyboard.type(`[[${marker}`)

  const pop = page.locator('.cm-tooltip-autocomplete')
  await expect(pop).toBeVisible({ timeout: 10_000 })
  await expect(pop.getByText(marker, { exact: false }).first()).toBeVisible()

  // CodeMirror refuses an accept within its interactionDelay (75ms) of the
  // popup opening, so a keystroke already in flight cannot pick an option
  // the user has not seen.
  await page.waitForTimeout(200)
  await page.keyboard.press('Enter')

  // The [[query range is replaced by the backticked 8-hex chip, in the
  // SOURCE -- which is what the reader turns into a clickable entity.
  await expect(async () => {
    expect(await readSource(page)).toMatch(/^`[0-9a-f]{8}` $/)
  }).toPass({ timeout: 5_000 })
})
