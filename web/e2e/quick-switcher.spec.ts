import { test, expect, type Page } from '@playwright/test'
import { E2E_EMAIL as EMAIL, E2E_PASSWORD as PASSWORD } from './global-setup'

// ADR-0038 layer D (task ec8e141a): the global Cmd/Ctrl+K quick-switcher.
// Its headline is the id branch — typing the 8-char hex CODE of a task
// actually FINDS it (the /tasks + /notes free-text boxes never matched
// the id column, which is exactly the discoverability gap roadmap notes
// hit). This spec drives the real stack and covers: open/close from any
// page, the instant client-side title filter, the id-branch code badge
// with the matched prefix highlighted, Enter-navigation, and the Recent
// section populated by a palette navigation. Asserts by CSS class so it
// is independent of the active UI language.

async function login(page: Page) {
  await page.goto('/login')
  await page.locator('input[type=email]').fill(EMAIL)
  await page.locator('input[type=password]').fill(PASSWORD)
  await page.locator('button[type=submit]').click()
  await page.waitForURL('**/notes', { timeout: 15_000 })
}

// Quick-add a task via the /tasks form and return its id, read from the
// detail-route URL. The client select auto-fills from /clients (the
// Personal default); wait for it so the create does not race the
// /tags load (mirrors app.spec's recent-widget setup).
async function createTask(page: Page, title: string): Promise<string> {
  await page.goto('/tasks')
  const form = page.locator('form.quickadd')
  await expect(form.locator('select[required]')).not.toHaveValue('')
  await form.locator('.quickadd__title').fill(title)
  await form.locator('button[type=submit]').click()
  const row = page.locator('.recentlist .recentrow__title', { hasText: title })
  await expect(row).toBeVisible({ timeout: 10_000 })
  await row.click()
  await page.waitForURL(/\/tasks\/[0-9a-f-]{36}/, { timeout: 10_000 })
  const m = page.url().match(/\/tasks\/([0-9a-f-]{36})/)
  if (!m) throw new Error(`no task id in URL: ${page.url()}`)
  return m[1]
}

test('Cmd+K quick-switcher: open/close, title filter, id branch, Recent', async ({
  page,
}) => {
  await login(page)
  const stamp = Date.now()
  const title = `QS task ${stamp}`
  const id = await createTask(page, title)
  const prefix = id.replace(/-/g, '').slice(0, 8)
  const pal = page.locator('.cmdk')

  // Opens from the task-detail page with the input focused; Escape closes.
  await page.keyboard.press('ControlOrMeta+k')
  await expect(pal).toBeVisible()
  await expect(pal.locator('.cmdk__input')).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(pal).toBeHidden()

  // Instant title filter: typing the title surfaces the task as a row.
  await page.keyboard.press('ControlOrMeta+k')
  await expect(pal).toBeVisible()
  await pal.locator('.cmdk__input').fill(title)
  await expect(pal.locator('.cmdk__row', { hasText: title })).toBeVisible({
    timeout: 10_000,
  })
  await page.keyboard.press('Escape')
  await expect(pal).toBeHidden()

  // Id branch (the headline) — opens from a *different* page too. Pasting
  // the 8-char hex code resolves the task via /lookup; the code badge
  // shows the matched prefix highlighted, and Enter navigates to it.
  await page.goto('/notes')
  await page.keyboard.press('ControlOrMeta+k')
  await expect(pal).toBeVisible()
  await pal.locator('.cmdk__input').fill(prefix)
  const idRow = pal.locator('.cmdk__row', { hasText: title })
  await expect(idRow).toBeVisible({ timeout: 10_000 })
  await expect(idRow.locator('.cmdk__code mark.cmdk__hl')).toHaveText(prefix)
  await page.keyboard.press('Enter')
  await page.waitForURL(new RegExp(`/tasks/${id}`), { timeout: 10_000 })

  // Recent: the palette navigation pushed the task to recents, so
  // reopening with an empty query lists it under the Recent section.
  await page.keyboard.press('ControlOrMeta+k')
  await expect(pal).toBeVisible()
  await expect(pal.locator('.cmdk__input')).toHaveValue('')
  await expect(pal.locator('.cmdk__row', { hasText: title })).toBeVisible({
    timeout: 10_000,
  })
  await page.keyboard.press('Escape')
  await expect(pal).toBeHidden()
})
