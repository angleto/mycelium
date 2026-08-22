import { test, expect, type Page } from '@playwright/test'
import { E2E_EMAIL as EMAIL, E2E_PASSWORD as PASSWORD } from './global-setup'
import { authedApi } from './api'

// The workspace lifecycle from the UI: pick one in the sidebar, create
// one there, and destroy one through the confirmation dialog.
//
// Deletion in particular could not be tested at all before: it was
// gated on `window.confirm`, which Playwright auto-DISMISSES unless a
// spec installs a dialog handler, so a click on Delete exercised
// nothing and passed. An in-DOM dialog is what makes the destructive
// path assertable — including the half that matters most, that it does
// NOT fire until the user has named the workspace.
//
// Locators are CSS classes wherever possible, so the suite does not
// depend on the active UI language.

test.describe.configure({ mode: 'serial' })

async function login(page: Page) {
  await page.goto('/login')
  await page.locator('input[type=email]').fill(EMAIL)
  await page.locator('input[type=password]').fill(PASSWORD)
  await page.locator('button[type=submit]').click()
  await page.waitForURL('**/notes', { timeout: 15_000 })
}

const trigger = (page: Page) => page.locator('.wssw__trigger')
// The NAME only: the trigger itself also carries the caret (and an
// "archived" tag when it applies), which is not part of the workspace.
const activeName = (page: Page) => page.locator('.wssw__name')
const dialog = (page: Page) => page.locator('.modal__backdrop[role="dialog"]')

async function openSwitcher(page: Page) {
  await expect(trigger(page)).toBeVisible({ timeout: 15_000 })
  await trigger(page).click()
  await expect(page.locator('.wssw__menu')).toBeVisible()
}

test('the sidebar switcher creates a workspace and moves into it', async ({
  page,
}) => {
  const name = `Switch WS ${Date.now()}`
  const a = await authedApi()
  try {
    await login(page)
    // Wait for the real name: the trigger renders a "…" placeholder
    // until the roster and the active-workspace fetch have landed, and
    // capturing THAT as the name to come back to is how this spec first
    // hung looking for a workspace called "…".
    await expect(activeName(page)).not.toHaveText('…', { timeout: 15_000 })
    const before = (await activeName(page).textContent())!.trim()

    await openSwitcher(page)
    // The create row turns into an inline form; the new workspace
    // becomes the active one (the whole authenticated subtree remounts).
    await page.locator('.wssw__row', { hasText: 'New workspace' }).click()
    await page.locator('.wssw__create input').fill(name)
    await page.locator('.wssw__create button[type=submit]').click()
    await expect(activeName(page)).toHaveText(name, { timeout: 15_000 })

    // And back again, by name.
    await openSwitcher(page)
    await page.locator('.wssw__row', { hasText: before }).first().click()
    await expect(activeName(page)).toHaveText(before, { timeout: 15_000 })
  } finally {
    // Whatever happened above, do not leave a workspace behind: every
    // run would otherwise add one to the switcher for good.
    const ws = await (
      await a.ctx.get('/workspaces', { headers: a.headers })
    ).json()
    const mine = ws.find((w: { name: string }) => w.name === name)
    if (mine) await a.ctx.delete(`/workspaces/${mine.id}`, { headers: a.headers })
    await a.ctx.dispose()
  }
})

test('deleting a workspace demands its name and warns what is lost', async ({
  page,
}) => {
  const name = `Doomed WS ${Date.now()}`
  const a = await authedApi()
  let id: string | null = null
  try {
    id = (
      await (
        await a.ctx.post('/workspaces', { headers: a.headers, data: { name } })
      ).json()
    ).id as string

    await login(page)
    await page.goto('/settings/workspace')

    const row = page.locator('.cpitem', { hasText: name })
    await expect(row).toBeVisible({ timeout: 15_000 })
    await row.locator('.btn--danger').click()

    // The dialog names the workspace and lists what goes with it.
    await expect(dialog(page)).toBeVisible()
    await expect(dialog(page)).toContainText(name)
    await expect(dialog(page).locator('.confirm__what li')).toHaveCount(4)

    // Armed only by the exact name.
    const confirm = dialog(page).locator('.modal__foot button[type=submit]')
    const proof = dialog(page).locator('.modal__body input')
    await expect(confirm).toBeDisabled()
    await proof.fill(`${name} nope`)
    await expect(confirm).toBeDisabled()

    // Escape abandons the whole thing, typed proof included.
    await page.keyboard.press('Escape')
    await expect(dialog(page)).toHaveCount(0)
    await expect(row).toBeVisible()

    // Second time through: name it, confirm, and the row is gone.
    await row.locator('.btn--danger').click()
    await dialog(page).locator('.modal__body input').fill(name)
    await expect(
      dialog(page).locator('.modal__foot button[type=submit]'),
    ).toBeEnabled()
    await dialog(page).locator('.modal__foot button[type=submit]').click()
    await expect(dialog(page)).toHaveCount(0, { timeout: 15_000 })
    await expect(page.locator('.cpitem', { hasText: name })).toHaveCount(0)

    // Really gone server-side, not just hidden.
    const ws = await (
      await a.ctx.get('/workspaces', { headers: a.headers })
    ).json()
    expect(ws.some((w: { id: string }) => w.id === id)).toBe(false)
    id = null
  } finally {
    if (id) await a.ctx.delete(`/workspaces/${id}`, { headers: a.headers })
    await a.ctx.dispose()
  }
})

test('archiving hides a workspace from the switcher and restoring brings it back', async ({
  page,
}) => {
  const name = `Shelf WS ${Date.now()}`
  const a = await authedApi()
  let id: string | null = null
  try {
    id = (
      await (
        await a.ctx.post('/workspaces', { headers: a.headers, data: { name } })
      ).json()
    ).id as string

    await login(page)
    await page.goto('/settings/workspace')
    const row = page.locator('.cpitem', { hasText: name })
    await expect(row).toBeVisible({ timeout: 15_000 })

    // Archive it (we are not in it, so no confirmation is asked for).
    await row.locator('.btn--ghost', { hasText: 'Archive' }).click()

    // Gone from the switcher, and from the main list — it is behind the
    // "show archived" toggle now.
    await openSwitcher(page)
    await expect(page.locator('.wssw__row', { hasText: name })).toHaveCount(0)
    await page.keyboard.press('Escape')

    const toggle = page.locator('label.row input[type=checkbox]').first()
    await expect(toggle).toBeVisible()
    await toggle.check()
    const archivedRow = page.locator('.cpitem', { hasText: name })
    await expect(archivedRow).toBeVisible()

    // Restore, and it is offered again.
    await archivedRow.locator('.btn--ghost', { hasText: 'Restore' }).click()
    await openSwitcher(page)
    await expect(page.locator('.wssw__row', { hasText: name })).toBeVisible()
  } finally {
    if (id) await a.ctx.delete(`/workspaces/${id}`, { headers: a.headers })
    await a.ctx.dispose()
  }
})
