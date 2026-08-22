import { test, expect, type Page } from '@playwright/test'
import { E2E_EMAIL as EMAIL, E2E_PASSWORD as PASSWORD } from './global-setup'
import { authedApi } from './api'

// Task 805a569c: "vedo progetti e clienti di un workspace diverso da quello
// selezionato".
//
// The focus (the sidebar client/project scope) used to be persisted under
// three FLAT localStorage keys shared by every workspace, and switching
// workspace is an in-app context switch rather than a reload -- so after a
// switch the picker went on displaying the previous tenant's client NAME, its
// id was mirrored into the Tasks quick-add, and every scoped view silently
// filtered down to nothing because that id matches no tag in the workspace you
// are actually in.
//
// What this spec pins is the guarantee that makes the leak unrepresentable
// rather than unlikely: a focus is a property of the ACTIVE WORKSPACE.
// Switching away must not carry it; switching back must restore the one that
// belongs there. Locators are CSS classes, so the suite is independent of the
// active UI language.

async function login(page: Page) {
  await page.goto('/login')
  await page.locator('input[type=email]').fill(EMAIL)
  await page.locator('input[type=password]').fill(PASSWORD)
  await page.locator('button[type=submit]').click()
  await page.waitForURL('**/notes', { timeout: 15_000 })
}

/** The sidebar focus client control (a search box, not a select). */
function focusClient(page: Page) {
  return page.locator('.focus input.csearch__input')
}

/** Type a needle into the focus client search and wait for the list to settle
 *  on the given expected row (or on its absence, once `settled` is visible). */
async function searchFocusClient(page: Page, needle: string) {
  const box = focusClient(page)
  await box.click()
  await box.fill(needle)
}

async function pickFocusClient(page: Page, name: string) {
  await searchFocusClient(page, name)
  const row = page.locator('.csearch__row', { hasText: name })
  await expect(row).toBeVisible({ timeout: 10_000 })
  await row.click()
  await expect(focusClient(page)).toHaveValue(name)
}

// Switching happens in the sidebar now (the settings page no longer
// carries a duplicate <select>), so the spec drives the real control.
async function switchWorkspace(page: Page, name: string) {
  const trigger = page.locator('.wssw__trigger')
  await expect(trigger).toBeVisible({ timeout: 10_000 })
  await trigger.click()
  const row = page.locator('.wssw__row', { hasText: name })
  await expect(row).toBeVisible({ timeout: 10_000 })
  await row.click()
  await expect(trigger).toContainText(name, { timeout: 10_000 })
}

test('focus does not leak across workspaces', async ({ page }) => {
  const stamp = Date.now()
  const wsAName = `Focus WS A ${stamp}`
  const wsBName = `Focus WS B ${stamp}`
  const clientA = `ClientA${stamp}`
  const clientB = `ClientB${stamp}`

  // Both workspaces are created by the spec rather than reusing the admin's
  // home one: GET /workspaces is name-ordered, so which workspace the SPA
  // activates on login depends on what previous runs left behind.
  const a = await authedApi()
  const created: string[] = []
  const makeWorkspace = async (name: string): Promise<string> => {
    const res = await a.ctx.post('/workspaces', { headers: a.headers, data: { name } })
    expect(res.status(), await res.text()).toBeLessThan(300)
    const id = (await res.json()).id as string
    created.push(id)
    return id
  }
  const makeClient = async (wsId: string, name: string) => {
    // POST /clients is owner-gated, hence the elevation header.
    const res = await a.ctx.post('/clients', {
      headers: { ...a.headers, 'X-Workspace-Id': wsId, 'X-Workspace-Role': 'owner' },
      data: { name, legal_name: name },
    })
    expect(res.status(), await res.text()).toBeLessThan(300)
  }

  const wsAId = await makeWorkspace(wsAName)
  const wsBId = await makeWorkspace(wsBName)
  await makeClient(wsAId, clientA)
  await makeClient(wsBId, clientB)

  try {
    await login(page)

    // Workspace A, focused on its own client.
    await switchWorkspace(page, wsAName)
    await pickFocusClient(page, clientA)

    // Switching tenant must CLEAR the control, not merely leave a stale id
    // unmatched by the new workspace's project list.
    await switchWorkspace(page, wsBName)
    await expect(focusClient(page)).toHaveValue('')

    // The search here reaches this workspace's clients...
    await searchFocusClient(page, clientB)
    await expect(page.locator('.csearch__row', { hasText: clientB })).toBeVisible({
      timeout: 10_000,
    })
    // ...and only those: retyping the OTHER workspace's client drops the row
    // that was just there and offers nothing in its place.
    await searchFocusClient(page, clientA)
    await expect(page.locator('.csearch__row', { hasText: clientB })).toHaveCount(0, {
      timeout: 10_000,
    })
    await expect(page.locator('.csearch__row', { hasText: clientA })).toHaveCount(0)

    // A focus set here belongs here...
    await pickFocusClient(page, clientB)

    // ...and switching back restores A's own focus rather than carrying B's.
    await switchWorkspace(page, wsAName)
    await expect(focusClient(page)).toHaveValue(clientA)

    // Still per-workspace after a reload: the record is keyed on disk too.
    await page.reload()
    await expect(focusClient(page)).toHaveValue(clientA)
  } finally {
    // Keep the switcher from growing two workspaces per run.
    for (const id of created) {
      await a.ctx.post(`/workspaces/${id}/archive`, { headers: a.headers })
    }
    await a.ctx.dispose()
  }
})
