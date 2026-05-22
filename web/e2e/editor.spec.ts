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
  // The create modal closes and the edit modal (with Attachments) opens;
  // give the autosave/remount a beat to settle before driving the editor.
  await expect(page.locator('.rte').first()).toBeVisible()
  await page.waitForTimeout(1000)
}

// The mode toggle is the only direct-child <button> of .rte__bar (the
// rest live in .rte__tools): robust to the label flipping between
// "Edit as Markdown" and "Rich editor".
const toggleBtn = (page: Page) => page.locator('.rte__bar > button').first()

async function enterMarkdownMode(page: Page) {
  if (!(await page.locator('textarea.rte__raw').isVisible())) {
    await toggleBtn(page).click()
    await page.waitForTimeout(300)
  }
  await expect(page.locator('textarea.rte__raw')).toBeVisible()
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
  await page.locator('textarea.rte__raw').fill(md)
  await toggleBtn(page).click() // -> WYSIWYG
  // WYSIWYG renders a real table.
  await expect(page.locator('.ProseMirror table')).toBeVisible()
  expect(await page.locator('.ProseMirror th').count()).toBe(2)
  // Back to markdown: serializes to a pipe table again.
  await toggleBtn(page).click()
  await expect(page.locator('textarea.rte__raw')).toBeVisible()
  const back = await page.locator('textarea.rte__raw').inputValue()
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
