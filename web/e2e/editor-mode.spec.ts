import { test, expect, type Page } from '@playwright/test'
import { E2E_EMAIL as EMAIL, E2E_PASSWORD as PASSWORD } from './global-setup'
import { editorMode, readSource, setEditorMode, setSource, sourceContent } from './source-editor'

// The toggle between the two views, in a real browser.
//
// The unit suite proves the transaction carries no document change. What only
// this level can prove is the consequence the owner of a note actually cares
// about: switching views does not touch the STORED body. The autosave is
// string-equality gated and debounced, so the assertion is that no write goes
// out at all, well past the debounce.

const VERBATIM = [
  '# Titolo',
  '',
  'Un paragrafo avvolto a mano attorno alle 72 colonne,',
  'come in un file tenuto in repository.',
  '',
  '| File | Cosa copre |',
  '|------|------------|',
  '| [`00-overview.md`](00-overview.md) | stato [proven] |',
  '',
].join('\n')

async function login(page: Page) {
  await page.goto('/login')
  await page.locator('input[type=email]').fill(EMAIL)
  await page.locator('input[type=password]').fill(PASSWORD)
  await page.locator('button[type=submit]').click()
  await page.waitForURL('**/notes', { timeout: 15_000 })
}

async function openFreshNoteEditor(page: Page) {
  await page.getByRole('button', { name: 'New note' }).click()
  await page.locator('.modal__panel input').first().fill(`e2e mode ${Date.now()}`)
  await page.locator('.modal__foot button:not(.btn--ghost)').first().click()
  await expect(page.locator('.parts-editor')).toBeVisible({ timeout: 10_000 })
  await page.getByRole('button', { name: 'Add part' }).click()
  await expect(page.locator('.rte').first()).toBeVisible({ timeout: 10_000 })
  await page.waitForTimeout(1000)
}

test('switching views writes nothing and changes no byte', async ({ page }) => {
  await login(page)
  await openFreshNoteEditor(page)
  await expect(sourceContent(page)).toBeVisible({ timeout: 10_000 })
  await setEditorMode(page, 'visual')
  await setSource(page, VERBATIM)
  // Let the 1.2s debounced autosave land what we typed.
  await page.waitForResponse(
    (r) => /\/notes\/[^/]+\/parts\/[^/]+$/.test(r.url()) && r.request().method() === 'PATCH',
    { timeout: 15_000 },
  )

  // From here on NOTHING may write.
  const writes: string[] = []
  page.on('request', (r) => {
    if (r.url().includes('/parts/') && ['PATCH', 'PUT', 'POST'].includes(r.method())) {
      writes.push(`${r.method()} ${r.url()}`)
    }
  })

  await setEditorMode(page, 'source')
  expect(await editorMode(page)).toBe('source')
  // Plain markdown: no preview decoration anywhere on the surface.
  expect(await page.locator('.rte__src [class*="cm-md-"]').count()).toBe(0)
  expect(await readSource(page)).toBe(VERBATIM)

  await setEditorMode(page, 'visual')
  expect(await editorMode(page)).toBe('visual')
  // The rendered view is back: the table is drawn rather than spelled out.
  await page.locator('.cm-content').first().click()
  await page.keyboard.press('ControlOrMeta+ArrowDown')
  await expect(page.locator('.cm-md-table table').first()).toBeVisible()
  expect(await readSource(page)).toBe(VERBATIM)

  // Well past the autosave debounce.
  await page.waitForTimeout(3500)
  expect(writes).toEqual([])
})

test('the choice is remembered across a reload', async ({ page }) => {
  await login(page)
  await openFreshNoteEditor(page)
  await expect(sourceContent(page)).toBeVisible({ timeout: 10_000 })
  await setEditorMode(page, 'source')
  await page.reload()
  await expect(sourceContent(page)).toBeVisible({ timeout: 10_000 })
  expect(await editorMode(page)).toBe('source')
  // Leave the suite in the view every other spec assumes.
  await setEditorMode(page, 'visual')
})
