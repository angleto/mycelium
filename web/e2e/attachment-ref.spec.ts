import { test, expect, type Page } from '@playwright/test'
import { E2E_EMAIL as EMAIL, E2E_PASSWORD as PASSWORD } from './global-setup'
import { inSourceMode, readSource, setSource, sourceContent } from './source-editor'

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

// Markdown <-> WYSIWYG toggle. It lives in `.rte__actions` and carries no
// title; its label flips between "Edit as Markdown" and "Rich editor". The
// old `.rte__bar > button` selector broke when the toolbar became
// collapsible (the bar's buttons are now nested under .rte__bar-left /
// .rte__tools, and that first button is the collapse toggle, not this one).
async function toggleEditorMode(page: Page) {
  await page
    .locator('.rte__actions button')
    .filter({ hasText: /Edit as Markdown|Rich editor/i })
    .first()
    .click()
  await page.waitForTimeout(300)
}

async function enterMarkdownMode(page: Page) {
  if (!(await inSourceMode(page))) {
    await toggleEditorMode(page)
  }
  await expect(sourceContent(page)).toBeVisible()
}

test('attachment link survives the markdown round-trip (keystone)', async ({
  page,
}) => {
  await login(page)
  await openFreshNoteEditor(page)
  await enterMarkdownMode(page)
  const id = '11111111-1111-1111-1111-111111111111'
  const href = `/attachments/${id}/download`
  await setSource(page, `[doc.pdf](${href})`)
  // -> WYSIWYG: the Link mark must be kept by validate (the fix), so the
  // parsed prose carries a real anchor with the relative href.
  await toggleEditorMode(page)
  await expect(
    page.locator(`.ProseMirror a[href="${href}"]`),
  ).toBeVisible()
  // -> back to markdown: the link must serialize back intact, not as the
  // bare word "doc.pdf" (the pre-fix failure: silent link loss).
  await toggleEditorMode(page)
  await expect(sourceContent(page)).toBeVisible()
  const back = await readSource(page)
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
  await setSource(page, '[the figure](Fig02_donne.png)')
  await toggleEditorMode(page)
  await expect(
    page.locator('.ProseMirror a[href="Fig02_donne.png"]'),
  ).toBeVisible()
  await toggleEditorMode(page)
  await expect(sourceContent(page)).toBeVisible()
  const back = await readSource(page)
  expect(back).toContain('[the figure](Fig02_donne.png)')
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
