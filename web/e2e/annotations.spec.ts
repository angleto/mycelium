import { test, expect, type Page, request as pwRequest } from '@playwright/test'
import { E2E_EMAIL as EMAIL, E2E_PASSWORD as PASSWORD } from './global-setup'

// Regression: the task-description annotation editor (RichEditor +
// AnnotationDecorations + the ✎/💬 toolbar actions + AnnotationsPanel)
// must mount without a render loop, and the suggesting flow must work
// end to end — select → ✎ opens the form → Propose renders the struck
// original + coloured proposal inline → Accept splices it into the body.
// A React infinite loop prints "Maximum update depth exceeded"
// (captured below); a CPU-pegging loop makes the actions time out.
// Covers the v2.0.57 regression (toolbar onClick blurred the editor and
// collapsed the selection; the buttons did nothing).

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

async function createTask(): Promise<string> {
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
    await ctx.post('/tasks', {
      headers,
      data: { title: `e2e anno ${Date.now()}`, description: 'The quick brown fox jumps.' },
    })
  ).json()
  await ctx.dispose()
  return task.id as string
}

test('task annotation editor mounts cleanly and ✎ opens the suggest form', async ({
  page,
}) => {
  const errors: string[] = []
  watch(page, errors)
  const taskId = await createTask()
  await login(page)
  await page.goto(`/tasks/${taskId}`)

  const editor = page.locator('.ProseMirror').first()
  await expect(editor).toBeVisible({ timeout: 15_000 })
  await expect(editor).toContainText('quick', { timeout: 10_000 })
  // A render loop would flood the console during this settle window.
  await page.waitForTimeout(2500)
  expect(errors, `errors after mount:\n${errors.join('\n')}`).toEqual([])

  // Select all text in the description editor, then click ✎ (suggest).
  await editor.click()
  await page.keyboard.press('ControlOrMeta+a')
  const suggestBtn = page.locator('.rte__actions button', { hasText: '✎' }).first()
  await expect(suggestBtn).toBeVisible()
  await suggestBtn.click()
  await page.waitForTimeout(1000)

  expect(errors, `errors after ✎ click:\n${errors.join('\n')}`).toEqual([])
  const sug = page.locator('.anno-panel__suggest').first()
  await expect(sug).toBeVisible({ timeout: 5000 })

  // Propose a replacement (input order: original[0], proposed[1], why[2]).
  await sug.locator('input').nth(1).fill('A lazy dog rests.')
  await sug.getByRole('button', { name: /Propose|Proponi/i }).click()
  await page.waitForTimeout(1000)
  expect(errors, `errors after propose:\n${errors.join('\n')}`).toEqual([])

  // The suggestion renders inline: struck original + coloured proposed.
  await expect(page.locator('.anno-mark--del').first()).toBeVisible({ timeout: 5000 })
  await expect(page.locator('.anno-mark--ins').first()).toBeVisible()

  // Accept it: the proposed text is spliced into the description.
  await page.locator('.anno button', { hasText: /Accept|Accetta/i }).first().click()
  await page.waitForTimeout(1500)
  expect(errors, `errors after accept:\n${errors.join('\n')}`).toEqual([])
  await expect(editor).toContainText('lazy dog', { timeout: 8000 })
})
