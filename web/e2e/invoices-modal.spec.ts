import { test, expect, type Page } from '@playwright/test'
import { E2E_EMAIL as EMAIL, E2E_PASSWORD as PASSWORD } from './global-setup'
import { authedApi } from './api'

// The open invoice used to render inline at the BOTTOM of the /invoices page,
// forcing a scroll to the end of a long list to read it. It now opens in a
// modal dialog (usability fix). This guards that behaviour: opening surfaces
// the modal (no inline ``.card--running`` detail), and every dismissal path
// (Close button, Escape, backdrop click) closes it.

const BENIGN = [
  /favicon/i,
  /\[vite\]/i,
  /Failed to load resource: the server responded with a status of 4\d\d/i,
]

function watch(page: Page, errors: string[]) {
  page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`))
  page.on('console', (m) => {
    if (m.type() !== 'error') return
    const t = m.text()
    if (!BENIGN.some((re) => re.test(t))) errors.push(`console.error: ${t}`)
  })
}

async function login(page: Page) {
  await page.goto('/login')
  await page.locator('input[type=email]').fill(EMAIL)
  await page.locator('input[type=password]').fill(PASSWORD)
  await page.locator('button[type=submit]').click()
  await page.waitForURL('**/notes', { timeout: 15_000 })
}

// Seed an issuer profile + client + a one-line draft via the API so the
// /invoices list has a row to open (mirrors the api-test seed).
async function seedInvoice(): Promise<void> {
  // Creating an issuer profile / client / invoice is owner-gated.
  const { ctx, headers } = await authedApi({ 'X-Workspace-Role': 'owner' })

  await ctx.post('/issuer-profiles', {
    headers,
    data: {
      label: `E2E DI ${Date.now()}`,
      legal_name: 'Mario Rossi',
      tax_code: 'RSSMRA80A01H501U',
      vat_number: '01112223334',
      tax_regime: 'RF19',
      address: 'Via Giuseppe Verdi 77',
      postal_code: '10154',
      city: 'Torino',
      province: 'TO',
      default_iban: 'IT60X0542811101000000123456',
    },
  })
  const client = await (
    await ctx.post('/clients', {
      headers,
      data: {
        name: `E2E Cylock ${Date.now()}`,
        legal_name: 'EXAMPLE S.R.L.',
        country_code: 'IT',
        vat_number: '02223334445',
        tax_code: '02223334445',
        address: 'Via Fonte Buono 19/B',
        postal_code: '00142',
        city: 'Roma',
        province: 'RM',
        sdi_code: 'WXYZ123',
      },
    })
  ).json()
  const inv = await (
    await ctx.post('/invoices', {
      headers,
      data: { client_tag_id: client.id, year: 2026, series: 'A' },
    })
  ).json()
  await ctx.post(`/invoices/${inv.id}/lines`, {
    headers,
    data: { description: 'Consulenza E2E', unit_price: '1000.00', quantity: '1' },
  })
  await ctx.dispose()
}

test('open invoice shows in a modal, not inline; closes via button / Esc / backdrop', async ({
  page,
}) => {
  const errors: string[] = []
  watch(page, errors)
  await seedInvoice()
  await login(page)
  await page.goto('/invoices')

  // The list has a row; no detail is shown yet.
  const openBtn = page.locator('.list li button').first()
  await expect(openBtn).toBeVisible({ timeout: 15_000 })
  const modal = page.locator('.modal__backdrop[role="dialog"]')
  await expect(modal).toHaveCount(0)

  // Open -> the detail is in a modal dialog, NOT an inline card at page bottom.
  await openBtn.click()
  await expect(modal).toBeVisible()
  await expect(modal.locator('.modal__head')).toContainText('/2026/')
  await expect(page.locator('.card--running')).toHaveCount(0)

  // Dismiss 1: the Close button in the header.
  await modal.locator('.modal__head button').click()
  await expect(modal).toHaveCount(0)

  // Dismiss 2: Escape.
  await openBtn.click()
  await expect(modal).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(modal).toHaveCount(0)

  // Dismiss 3: a click on the backdrop (outside the panel).
  await openBtn.click()
  await expect(modal).toBeVisible()
  await modal.click({ position: { x: 5, y: 5 } })
  await expect(modal).toHaveCount(0)

  expect(errors, errors.join('\n')).toEqual([])
})
