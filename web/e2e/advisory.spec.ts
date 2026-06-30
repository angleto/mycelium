import { test, expect, type Page } from '@playwright/test'
// Neutral admin the globalSetup bootstraps (no personal data in the repo).
import { E2E_EMAIL as EMAIL, E2E_PASSWORD as PASSWORD } from './global-setup'

// A handled 4xx auto-logs "Failed to load resource"; the app surfaces it
// via the .err banner. 5xx is NOT filtered — a server crash (e.g. the
// tz-naive window_start 500 this suite guards against) must fail the gate.
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

test.describe.configure({ mode: 'serial' })

// (#1) The load-bearing regression: submitting with NO manual date sends a
// tz-aware now(); the old naive `${start}:00` 500s against the aware
// due_date. A 5xx would surface as a non-benign console.error and fail.
test('what-now: zero-input submit renders the ranked plan (no tz-naive 500)', async ({
  page,
}) => {
  const errors: string[] = []
  watch(page, errors)
  await login(page)
  await page.goto('/advisory')
  await page.getByRole('button', { name: /^(suggest|suggerisci)$/i }).click()
  // The results section renders (heading + list or 'none' hint); on a 500
  // it would not appear and an .err banner + 5xx error would show instead.
  await expect(page.getByRole('heading', { name: /feasible|fattibili/i })).toBeVisible()
  expect(errors, errors.join('\n')).toEqual([])
})

test('what-now: Advanced override reveals an editable start picker', async ({ page }) => {
  await login(page)
  await page.goto('/advisory')
  const picker = page.locator('input[type="datetime-local"]')
  // Collapsed by default (window_start defaults to now under the hood).
  await expect(picker).toHaveCount(0)
  await page.getByRole('button', { name: /advanced|avanzate/i }).click()
  await expect(picker).toBeVisible()
  await picker.fill('2026-01-12T09:00')
  await expect(picker).toHaveValue('2026-01-12T09:00')
})

test('what-now: Help me decide is opt-in and degrades gracefully with no LLM', async ({
  page,
}) => {
  const errors: string[] = []
  watch(page, errors)
  await login(page)
  await page.goto('/advisory')
  const unavailable = page.getByText(/AI advice is not available|consiglio AI non è disponibile/i)
  // No narration surface before the deterministic plan is requested.
  await expect(unavailable).toHaveCount(0)
  await page.getByRole('button', { name: /^(suggest|suggerisci)$/i }).click()
  await expect(page.getByRole('heading', { name: /feasible|fattibili/i })).toBeVisible()
  // Opt-in: the panel stays empty until the button is clicked.
  await expect(unavailable).toHaveCount(0)
  const help = page.getByRole('button', { name: /help me decide|aiutami a decidere/i })
  await expect(help).toBeVisible()
  await help.click()
  // No LLM configured in the e2e env -> graceful narrated=false state, and
  // the deterministic ranked list stays usable (not an error).
  await expect(unavailable).toBeVisible()
  await expect(page.getByRole('heading', { name: /feasible|fattibili/i })).toBeVisible()
  expect(errors, errors.join('\n')).toEqual([])
})
