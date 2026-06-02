import { test, expect, type Page } from '@playwright/test'
import { E2E_EMAIL as EMAIL, E2E_PASSWORD as PASSWORD } from './global-setup'

// Linkable attachments in the note/task body. The load-bearing risk is
// the tiptap Link `validate` predicate: a relative
// /attachments/<id>/download href must survive the markdown round-trip
// (insert -> serialize -> parse-back) instead of being silently demoted
// to bare text, which would lose the link on save/reload. Plus the
// toolbar affordance to link an attachment.

async function login(page: Page) {
  await page.goto('/login')
  await page.locator('input[type=email]').fill(EMAIL)
  await page.locator('input[type=password]').fill(PASSWORD)
  await page.locator('button[type=submit]').click()
  await page.waitForURL('**/notes', { timeout: 15_000 })
}

async function openFreshNoteEditor(page: Page) {
  await page.getByRole('button', { name: 'New note' }).click()
  await page.locator('.modal__panel input').first().fill(`e2e attach ${Date.now()}`)
  await page.locator('.modal__foot button:not(.btn--ghost)').first().click()
  await expect(page.locator('.rte').first()).toBeVisible()
  await page.waitForTimeout(1000)
}

const toggleBtn = (page: Page) => page.locator('.rte__bar > button').first()

async function enterMarkdownMode(page: Page) {
  if (!(await page.locator('textarea.rte__raw').isVisible())) {
    await toggleBtn(page).click()
    await page.waitForTimeout(300)
  }
  await expect(page.locator('textarea.rte__raw')).toBeVisible()
}

test('attachment link survives the markdown round-trip (keystone)', async ({
  page,
}) => {
  await login(page)
  await openFreshNoteEditor(page)
  await enterMarkdownMode(page)
  const id = '11111111-1111-1111-1111-111111111111'
  const href = `/attachments/${id}/download`
  await page.locator('textarea.rte__raw').fill(`[doc.pdf](${href})`)
  // -> WYSIWYG: the Link mark must be kept by validate (the fix), so the
  // parsed prose carries a real anchor with the relative href.
  await toggleBtn(page).click()
  await expect(
    page.locator(`.ProseMirror a[href="${href}"]`),
  ).toBeVisible()
  // -> back to markdown: the link must serialize back intact, not as the
  // bare word "doc.pdf" (the pre-fix failure: silent link loss).
  await toggleBtn(page).click()
  await expect(page.locator('textarea.rte__raw')).toBeVisible()
  const back = await page.locator('textarea.rte__raw').inputValue()
  expect(back).toContain(`[doc.pdf](${href})`)
})

test('a bare-filename attachment link survives the round-trip', async ({
  page,
}) => {
  await login(page)
  await openFreshNoteEditor(page)
  await enterMarkdownMode(page)
  // A link whose href is a plain filename (an attachment of this note
  // referenced by name). Link.validate must keep the mark so it is not
  // demoted to bare text on parse-back.
  await page.locator('textarea.rte__raw').fill('[the figure](Fig02_donne.png)')
  await toggleBtn(page).click()
  await expect(
    page.locator('.ProseMirror a[href="Fig02_donne.png"]'),
  ).toBeVisible()
  await toggleBtn(page).click()
  await expect(page.locator('textarea.rte__raw')).toBeVisible()
  const back = await page.locator('textarea.rte__raw').inputValue()
  expect(back).toContain('[the figure](Fig02_donne.png)')
})

test('the attach/link toolbar button opens the attachment picker', async ({
  page,
}) => {
  await login(page)
  await openFreshNoteEditor(page)
  // The note is saved, so the parent exists and the button is enabled.
  const btn = page.locator('.rte__tools button[title="Attach / link a file"]')
  await expect(btn).toBeEnabled()
  await btn.click()
  await expect(page.locator('.attref')).toBeVisible()
})
