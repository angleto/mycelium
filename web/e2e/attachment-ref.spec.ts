import { test, expect, type Page } from '@playwright/test'
import { E2E_EMAIL as EMAIL, E2E_PASSWORD as PASSWORD } from './global-setup'
import { readSource, setSource, sourceContent } from './source-editor'

// Linkable attachments in the note/task body. The risk these tests were
// written for was a relative /attachments/<id>/download href being
// silently demoted to bare text by the round trip the document-model
// editor performed on every save. There is no round trip now, so what
// they assert is the destination surviving in the stored bytes and the
// preview reading it as a link. Plus the toolbar affordance to link an
// attachment.

async function login(page: Page) {
  await page.goto('/login')
  await page.locator('input[type=email]').fill(EMAIL)
  await page.locator('input[type=password]').fill(PASSWORD)
  await page.locator('button[type=submit]').click()
  await page.waitForURL('**/notes', { timeout: 15_000 })
}

// A note now edits its body as one-or-more *parts* (multi-part refactor):
// a freshly-created note has zero parts, so no RichEditor mounts until one
// is added. Create the note, then add a part so the body editor (and its
// toolbar) exist.
async function openFreshNoteEditor(page: Page) {
  await page.getByRole('button', { name: 'New note' }).click()
  await page
    .locator('.modal__panel input')
    .first()
    .fill(`e2e attach ${Date.now()}`)
  await page.locator('.modal__foot button:not(.btn--ghost)').first().click()
  const addPart = page.getByRole('button', { name: /add part/i }).first()
  await expect(addPart).toBeVisible()
  await addPart.click()
  await expect(page.locator('.rte').first()).toBeVisible()
  await page.waitForTimeout(800)
}

/** One surface now; this only waits for it. */
async function awaitSourceEditor(page: Page) {
  await expect(sourceContent(page)).toBeVisible({ timeout: 10_000 })
}

test('an attachment link keeps its destination (keystone)', async ({ page }) => {
  // The original failure was silent link LOSS: the WYSIWYG surface parsed
  // this, dropped the mark, and serialised back the bare word `doc.pdf`.
  // There is no parse and no serialise now, so the assertion is the one that
  // always mattered -- the bytes -- plus the preview reading it as a link.
  await login(page)
  await openFreshNoteEditor(page)
  await awaitSourceEditor(page)
  const id = '11111111-1111-1111-1111-111111111111'
  const href = `/attachments/${id}/download`
  const md = `[doc.pdf](${href})`
  await setSource(page, md)
  await page.locator('.cm-content').first().click()
  await page.keyboard.press('ControlOrMeta+ArrowDown')
  await expect(page.locator('.cm-md-linklabel').first()).toBeVisible()
  expect(await readSource(page)).toBe(md)
})

test('a bare-filename attachment link keeps its destination', async ({ page }) => {
  // A relative path with no directory separator: tiptap's default
  // isAllowedUri rejected the version WITH one, which is how this class of
  // link used to be demoted to bare text.
  await login(page)
  await openFreshNoteEditor(page)
  await awaitSourceEditor(page)
  const md = '[the figure](Fig02_donne.png)'
  await setSource(page, md)
  await page.locator('.cm-content').first().click()
  await page.keyboard.press('ControlOrMeta+ArrowDown')
  await expect(page.locator('.cm-md-linklabel').first()).toBeVisible()
  expect(await readSource(page)).toBe(md)
})

test('the attach/link toolbar button opens the attachment picker', async ({
  page,
}) => {
  await login(page)
  await openFreshNoteEditor(page)
  // The note is saved (parent exists) and a part is being edited, so the
  // attach button is present and enabled.
  const btn = page.locator('.rte__tools button[title="Attach / link a file"]')
  await expect(btn.first()).toBeEnabled()
  await btn.first().click()
  await expect(page.locator('.attref')).toBeVisible()
})
