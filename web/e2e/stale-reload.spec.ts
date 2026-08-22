import { test, expect, type Page } from '@playwright/test'
import { authedApi } from './api'
import { E2E_EMAIL as EMAIL, E2E_PASSWORD as PASSWORD } from './global-setup'

// The SPA loads a note/task once on open and never polls, so an
// out-of-band write (an MCP tool, the CLI, another device) is invisible
// until a manual refresh. useStaleWatch closes that gap: on tab focus it
// re-probes the server `version` and, when the server is ahead, raises a
// non-destructive "changed elsewhere → Reload" banner (RefreshHint).
//
// These cover the deterministic, no-local-edits path for both surfaces:
//   - task: an out-of-band description edit bumps task.version;
//   - note part: a part-body edit bumps only the PART version (the note
//     row is untouched), which is exactly the case a note-level version
//     check would miss — so the parts signature must catch it.
// The dirty-guard variant (warn + discard on reload) is timing-coupled
// to the 1s autosave debounce and is left to the manual test plan.

const BENIGN = [
  /favicon/i,
  /React DevTools/i,
  /ResizeObserver loop/i,
  /\[vite\]/i,
  /Failed to load resource: the server responded with a status of 4\d\d/i,
]

function watch(page: Page, errors: string[]) {
  page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`))
  page.on('console', (m) => {
    if (m.type() !== 'error') return
    const t = m.text()
    if (BENIGN.some((re) => re.test(t))) return
    errors.push(`console.error: ${t}`)
  })
}

async function login(page: Page) {
  await page.goto('/login')
  await page.locator('input[type=email]').fill(EMAIL)
  await page.locator('input[type=password]').fill(PASSWORD)
  await page.locator('button[type=submit]').click()
  await page.waitForURL('**/notes', { timeout: 15_000 })
}

// The staleness probe only fires on the same wake triggers the
// running-timer poll trusts. Returning to the tab is what we simulate.
async function refocus(page: Page) {
  await page.evaluate(() => {
    window.dispatchEvent(new Event('focus'))
    document.dispatchEvent(new Event('visibilitychange'))
  })
}

const reloadBtn = (page: Page) =>
  page.getByRole('button', { name: /^(Reload|Ricarica)$/ })

test('task: an out-of-band edit surfaces the reload banner and reload shows it', async ({
  page,
}) => {
  const errors: string[] = []
  watch(page, errors)
  const { ctx, headers } = await authedApi()
  const task = await (
    await ctx.post('/tasks', {
      headers,
      data: { title: `e2e stale ${Date.now()}`, description: 'original body' },
    })
  ).json()

  await login(page)
  await page.goto(`/tasks/${task.id}`)
  const editor = page.locator('.cm-content').first()
  await expect(editor).toContainText('original body', { timeout: 15_000 })
  await page.waitForTimeout(1000)

  // Out-of-band edit (stands in for an MCP write): read the live version,
  // then PATCH the description so the row version moves ahead of the SPA.
  const fresh = await (await ctx.get(`/tasks/${task.id}`, { headers })).json()
  const patched = await ctx.patch(`/tasks/${task.id}`, {
    headers,
    data: { expected_version: fresh.version, description: 'changed by mcp' },
  })
  expect(patched.ok()).toBeTruthy()

  // Silent until we come back to the tab: no banner on its own.
  await expect(reloadBtn(page)).toHaveCount(0)
  await refocus(page)

  await expect(reloadBtn(page)).toBeVisible({ timeout: 8000 })
  await reloadBtn(page).click()
  await expect(editor).toContainText('changed by mcp', { timeout: 8000 })
  await expect(editor).not.toContainText('original body')

  await ctx.dispose()
  expect(errors, `errors:\n${errors.join('\n')}`).toEqual([])
})

test('note part: an out-of-band part edit surfaces the reload banner and reload shows it', async ({
  page,
}) => {
  const errors: string[] = []
  watch(page, errors)
  const { ctx, headers } = await authedApi()
  const note = await (
    await ctx.post('/notes', {
      headers,
      data: { kind: 'text', title: `e2e stale note ${Date.now()}` },
    })
  ).json()
  await ctx.post(`/notes/${note.id}/parts`, {
    headers,
    data: { body: 'part original' },
  })

  await login(page)
  await page.goto(`/notes/${note.id}`)
  const editor = page.locator('.parts-editor .cm-content').first()
  await expect(editor).toContainText('part original', { timeout: 15_000 })
  // Let NotePartsEditor lift its parts signature before we move it.
  await page.waitForTimeout(1200)

  // Out-of-band part-body edit: bumps the PART version only, leaving the
  // note row untouched — the case the note-level check alone misses.
  const parts = await (
    await ctx.get(`/notes/${note.id}/parts`, { headers })
  ).json()
  const part = parts[0]
  const patched = await ctx.patch(`/notes/${note.id}/parts/${part.id}`, {
    headers,
    data: { expected_version: part.version, body: 'part changed by mcp' },
  })
  expect(patched.ok()).toBeTruthy()

  await refocus(page)
  await expect(reloadBtn(page)).toBeVisible({ timeout: 8000 })
  await reloadBtn(page).click()
  await expect(editor).toContainText('part changed by mcp', { timeout: 8000 })
  await expect(editor).not.toContainText('part original')

  await ctx.dispose()
  expect(errors, `errors:\n${errors.join('\n')}`).toEqual([])
})
