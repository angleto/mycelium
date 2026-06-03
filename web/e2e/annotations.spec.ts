import { test, expect, type Page, request as pwRequest } from '@playwright/test'
import { E2E_EMAIL as EMAIL, E2E_PASSWORD as PASSWORD } from './global-setup'

// Regression for the inline annotation UX (InlineAnnotator over the
// task-description RichEditor):
//   - select text → the editor's (sticky) toolbar 💬/✎ buttons enable;
//   - ✎ → a compose popover (NOT a panel at the bottom of the page);
//   - Propose → struck original + coloured proposal render inline;
//   - click the struck text → an action popover opens on the spot;
//   - Accept → the proposed text is SPLICED into the body and the editor
//     shows it.
// Covers the bugs the user hit:
//   * the toolbar buttons did nothing (selection lost on blur) — gone,
//     the handler reads the live editor selection;
//   * Accept did not replace the word — asserted below;
//   * having to scroll to the bottom panel — interaction is inline.
// A React render loop prints "Maximum update depth exceeded" (captured).

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

async function createTask(description: string): Promise<string> {
  const ctx = await pwRequest.newContext({ baseURL: 'http://localhost:8000' })
  const auth = await (
    await ctx.post('/auth/login', { data: { email: EMAIL, password: PASSWORD } })
  ).json()
  const token = auth.token as string
  const ws = await (
    await ctx.get('/workspaces', { headers: { Authorization: `Bearer ${token}` } })
  ).json()
  const headers = { Authorization: `Bearer ${token}`, 'X-Workspace-Id': ws[0].id }
  const task = await (
    await ctx.post('/tasks', { headers, data: { title: `e2e anno ${Date.now()}`, description } })
  ).json()
  await ctx.dispose()
  return task.id as string
}

async function createNoteWithPart(body: string): Promise<string> {
  const ctx = await pwRequest.newContext({ baseURL: 'http://localhost:8000' })
  const auth = await (
    await ctx.post('/auth/login', { data: { email: EMAIL, password: PASSWORD } })
  ).json()
  const token = auth.token as string
  const ws = await (
    await ctx.get('/workspaces', { headers: { Authorization: `Bearer ${token}` } })
  ).json()
  const headers = { Authorization: `Bearer ${token}`, 'X-Workspace-Id': ws[0].id }
  const note = await (
    await ctx.post('/notes', { headers, data: { kind: 'text', title: `e2e anno note ${Date.now()}` } })
  ).json()
  await ctx.post(`/notes/${note.id}/parts`, {
    headers: { ...headers, 'Content-Type': 'application/json' },
    data: { body },
  })
  await ctx.dispose()
  return note.id as string
}

async function fetchTaskDescription(taskId: string): Promise<string> {
  const ctx = await pwRequest.newContext({ baseURL: 'http://localhost:8000' })
  const auth = await (
    await ctx.post('/auth/login', { data: { email: EMAIL, password: PASSWORD } })
  ).json()
  const token = auth.token as string
  const ws = await (
    await ctx.get('/workspaces', { headers: { Authorization: `Bearer ${token}` } })
  ).json()
  const task = await (
    await ctx.get(`/tasks/${taskId}`, {
      headers: { Authorization: `Bearer ${token}`, 'X-Workspace-Id': ws[0].id },
    })
  ).json()
  await ctx.dispose()
  return (task.description ?? '') as string
}

test('inline suggest: toolbar → propose → inline diff → click mark → accept splices', async ({
  page,
}) => {
  const errors: string[] = []
  watch(page, errors)
  const taskId = await createTask('alpha beta gamma delta')
  await login(page)
  await page.goto(`/tasks/${taskId}`)

  const editor = page.locator('.ProseMirror').first()
  await expect(editor).toBeVisible({ timeout: 15_000 })
  await expect(editor).toContainText('beta', { timeout: 10_000 })
  await page.waitForTimeout(2000)
  expect(errors, `errors after mount:\n${errors.join('\n')}`).toEqual([])

  // Select the first word ("alpha") with the keyboard → the toolbar's
  // Suggest button enables; click it.
  await editor.click()
  await page.keyboard.press('Home')
  for (let i = 0; i < 5; i += 1) await page.keyboard.press('Shift+ArrowRight')

  const suggestBtn = page.locator('.rte__annotate--suggest').first()
  await expect(suggestBtn).toBeEnabled({ timeout: 5000 })
  await suggestBtn.click()

  // Compose popover (inline, not at page bottom).
  const pop = page.locator('.anno-pop').first()
  await expect(pop).toBeVisible({ timeout: 5000 })
  await pop.locator('textarea').first().fill('alphaX')
  await pop.getByRole('button', { name: /Propose|Proponi/i }).click()
  await page.waitForTimeout(800)
  expect(errors, `errors after propose:\n${errors.join('\n')}`).toEqual([])

  // Inline diff renders over the prose.
  const del = page.locator('.anno-mark--del').first()
  await expect(del).toBeVisible({ timeout: 5000 })
  await expect(page.locator('.anno-mark--ins').first()).toBeVisible()

  // Click the struck original → action popover → Accept.
  await del.click()
  const actPop = page.locator('.anno-pop').first()
  await expect(actPop).toBeVisible({ timeout: 5000 })
  await actPop.getByRole('button', { name: /Accept|Accetta/i }).click()
  await page.waitForTimeout(1800)
  expect(errors, `errors after accept:\n${errors.join('\n')}`).toEqual([])

  // The proposed text replaced the original in the editor.
  await expect(editor).toContainText('alphaX', { timeout: 8000 })
  await expect(editor).toContainText('beta gamma delta')
})

test('cancel discards a half-written suggestion', async ({ page }) => {
  const errors: string[] = []
  watch(page, errors)
  const taskId = await createTask('one two three four')
  await login(page)
  await page.goto(`/tasks/${taskId}`)

  const editor = page.locator('.ProseMirror').first()
  await expect(editor).toBeVisible({ timeout: 15_000 })
  await expect(editor).toContainText('two', { timeout: 10_000 })

  await editor.click()
  const suggestBtn = page.locator('.rte__annotate--suggest').first()
  // On slower CI the keyboard selection can fire before ProseMirror is
  // ready to report it, leaving the Suggest button disabled. Re-select
  // until the toolbar reflects the non-empty selection (deterministic,
  // no fixed settle) instead of asserting once on a possibly-lost one.
  await expect(async () => {
    await page.keyboard.press('Home')
    for (let i = 0; i < 3; i += 1) await page.keyboard.press('Shift+ArrowRight')
    await expect(suggestBtn).toBeEnabled({ timeout: 1000 })
  }).toPass({ timeout: 10_000 })
  await suggestBtn.click()

  const pop = page.locator('.anno-pop').first()
  await expect(pop).toBeVisible({ timeout: 5000 })
  await pop.locator('textarea').first().fill('changed my mind')
  await pop.getByRole('button', { name: /Cancel|Annulla/i }).click()

  // Popover gone, no suggestion created (no inline diff marks).
  await expect(page.locator('.anno-pop')).toHaveCount(0)
  await expect(page.locator('.anno-mark--del')).toHaveCount(0)
  expect(errors, `errors:\n${errors.join('\n')}`).toEqual([])
})

test('inline-mark suggestion: decoration spans the mark and accept keeps the markdown', async ({
  page,
}) => {
  const errors: string[] = []
  watch(page, errors)
  // 'beta' is rendered text INSIDE bold; the old single-text-node finder
  // could decorate it, but the old accept spliced the rendered quote into
  // the markdown and would corrupt/stale the **. md_anchor fixes accept;
  // renderDocWithMap keeps the decoration consistent.
  const taskId = await createTask('alpha **beta** gamma')
  await login(page)
  await page.goto(`/tasks/${taskId}`)

  const editor = page.locator('.ProseMirror').first()
  await expect(editor).toBeVisible({ timeout: 15_000 })
  await expect(editor).toContainText('beta', { timeout: 10_000 })
  await page.waitForTimeout(1500)

  // Select the bold word by double-clicking the <strong>.
  await editor.locator('strong').first().dblclick()
  const suggestBtn = page.locator('.rte__annotate--suggest').first()
  await expect(suggestBtn).toBeEnabled({ timeout: 5000 })
  await suggestBtn.click()
  const pop = page.locator('.anno-pop').first()
  await expect(pop).toBeVisible({ timeout: 5000 })
  await pop.locator('textarea').first().fill('DELTA')
  await pop.getByRole('button', { name: /Propose|Proponi/i }).click()
  await page.waitForTimeout(800)

  // The decoration draws over the bold word (multi-node rendered domain).
  const del = page.locator('.anno-mark--del').first()
  await expect(del).toBeVisible({ timeout: 5000 })
  await del.click()
  const actPop = page.locator('.anno-pop').first()
  await expect(actPop).toBeVisible({ timeout: 5000 })
  await actPop.getByRole('button', { name: /Accept|Accetta/i }).click()
  await page.waitForTimeout(1800)
  expect(errors, `errors:\n${errors.join('\n')}`).toEqual([])

  await expect(editor).toContainText('DELTA', { timeout: 8000 })
  // The crucial assertion: the stored markdown kept the bold delimiters.
  expect(await fetchTaskDescription(taskId)).toBe('alpha **DELTA** gamma')
})

test('inline suggest on a NOTE part: accept replaces the part text', async ({ page }) => {
  const errors: string[] = []
  watch(page, errors)
  const noteId = await createNoteWithPart('alpha beta gamma delta')
  await login(page)
  await page.goto(`/notes/${noteId}`)

  // The note opens in a modal; the part body is a RichEditor inside the
  // parts editor. This is the surface where #2 used to fail: after Accept
  // the local ``editingBody`` draft shadowed the freshly spliced body.
  const editor = page.locator('.parts-editor .ProseMirror').first()
  await expect(editor).toBeVisible({ timeout: 15_000 })
  await expect(editor).toContainText('beta', { timeout: 10_000 })
  await page.waitForTimeout(1500)

  // Select the whole part body (robust regardless of where the click
  // lands in a tall editor) → the toolbar's Suggest button enables.
  await editor.click()
  await page.keyboard.press('ControlOrMeta+a')
  const suggestBtn = page.locator('.rte__annotate--suggest').first()
  await expect(suggestBtn).toBeEnabled({ timeout: 5000 })
  await suggestBtn.click()

  const pop = page.locator('.anno-pop').first()
  await expect(pop).toBeVisible({ timeout: 5000 })
  await pop.locator('textarea').first().fill('REPLACED text')
  await pop.getByRole('button', { name: /Propose|Proponi/i }).click()
  await page.waitForTimeout(800)

  const del = page.locator('.anno-mark--del').first()
  await expect(del).toBeVisible({ timeout: 5000 })
  await del.click()
  const actPop = page.locator('.anno-pop').first()
  await expect(actPop).toBeVisible({ timeout: 5000 })
  await actPop.getByRole('button', { name: /Accept|Accetta/i }).click()
  await page.waitForTimeout(2200)

  // The spliced text must show in the part editor: the local draft no
  // longer shadows the reloaded body (the #2 note-path bug).
  await expect(editor).toContainText('REPLACED text', { timeout: 8000 })
  await expect(editor).not.toContainText('beta gamma delta')
})
