import { test, expect, type Page } from '@playwright/test'
import { E2E_EMAIL as EMAIL, E2E_PASSWORD as PASSWORD } from './global-setup'

// Regression coverage for the note rich editor (RichEditor + tiptap):
// the layout overlap, the <label>-forwarded double-click bold, and the
// GFM table round-trip — all bugs that reached production.

async function login(page: Page) {
  await page.goto('/login')
  await page.locator('input[type=email]').fill(EMAIL)
  await page.locator('input[type=password]').fill(PASSWORD)
  await page.locator('button[type=submit]').click()
  await page.waitForURL('**/notes', { timeout: 15_000 })
}

async function openFreshNoteEditor(page: Page) {
  await page.getByRole('button', { name: 'New note' }).click()
  await page.locator('.modal__panel input').first().fill(`e2e editor ${Date.now()}`)
  await page.locator('.modal__foot button:not(.btn--ghost)').first().click()
  // The note page opens with the multi-part editor. New notes start with
  // zero parts (notes are split into markdown blocks), so add one to get a
  // rich-text body editor (.rte) to drive.
  await expect(page.locator('.parts-editor')).toBeVisible({ timeout: 10_000 })
  await page.getByRole('button', { name: 'Add part' }).click()
  await expect(page.locator('.rte').first()).toBeVisible({ timeout: 10_000 })
  // Give the autosave/remount a beat to settle before driving the editor.
  await page.waitForTimeout(1000)
}

// The WYSIWYG<->Markdown mode toggle lives in the collapsible toolbar;
// its label flips between "Edit as Markdown" and "Rich editor".
const toggleBtn = (page: Page) =>
  page.getByRole('button', { name: /Edit as Markdown|Rich editor/ }).first()

// The toolbar tools (incl. the mode toggle) collapse behind the "Aa"
// button per a saved preference; expand them if the toggle is hidden.
async function ensureToolbar(page: Page) {
  if (!(await toggleBtn(page).isVisible().catch(() => false))) {
    await page.locator('.rte__collapse').first().click()
    await expect(toggleBtn(page)).toBeVisible()
  }
}

async function enterMarkdownMode(page: Page) {
  await ensureToolbar(page)
  if (!(await page.locator('textarea.rte__raw').first().isVisible().catch(() => false))) {
    await toggleBtn(page).click()
    await page.waitForTimeout(300)
  }
  await expect(page.locator('textarea.rte__raw').first()).toBeVisible()
}

test('rich editor is not wrapped in a <label> (double-click must not bold)', async ({
  page,
}) => {
  await login(page)
  await openFreshNoteEditor(page)
  // A <label> forwards user clicks to its first control (the Bold
  // button), so wrapping the editor made a double-click bold the word.
  const wrappedInLabel = await page.evaluate(
    () => !!document.querySelector('.rte')?.closest('label'),
  )
  expect(wrappedInLabel).toBe(false)
})

test('markdown table round-trips through the editor', async ({ page }) => {
  await login(page)
  await openFreshNoteEditor(page)
  await enterMarkdownMode(page)
  const md = ['| Name | Age |', '| --- | --- |', '| Alice | 30 |'].join('\n')
  await page.locator('textarea.rte__raw').first().fill(md)
  await toggleBtn(page).click() // -> WYSIWYG
  // WYSIWYG renders a real table.
  await expect(page.locator('.ProseMirror table').first()).toBeVisible()
  expect(await page.locator('.ProseMirror th').count()).toBe(2)
  // Back to markdown: serializes to a pipe table again.
  await toggleBtn(page).click()
  await expect(page.locator('textarea.rte__raw').first()).toBeVisible()
  const back = await page.locator('textarea.rte__raw').first().inputValue()
  expect(back).toContain('| Name')
  expect(back).toContain('| Alice')
})

test('in markdown mode the Attach-file block does not overlap the editor', async ({
  page,
}) => {
  await login(page)
  await openFreshNoteEditor(page)
  await enterMarkdownMode(page)
  await page
    .locator('textarea.rte__raw')
    .first()
    .fill(Array.from({ length: 60 }, (_, i) => `line ${i + 1}`).join('\n'))
  const box = await page.evaluate(() => {
    const ta = document.querySelector('textarea.rte__raw')!.getBoundingClientRect()
    const btn = document
      .querySelector('.atts')!
      .querySelector('button')!
      .getBoundingClientRect()
    return { taBottom: ta.bottom, attachTop: btn.top }
  })
  // The attach button starts at or below the editor's bottom edge.
  expect(box.attachTop).toBeGreaterThanOrEqual(box.taBottom - 1)
})
