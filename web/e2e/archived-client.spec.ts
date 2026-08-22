import { test, expect, type Page } from '@playwright/test'
import { E2E_EMAIL as EMAIL, E2E_PASSWORD as PASSWORD } from './global-setup'
import { authedApi } from './api'

// "Il dropdown dei client sul frontend mi mostra dei client che sono
// archived, non dovrebbe accadere."
//
// A client is a tag, and archiving one means "stop offering it". GET /tags
// had said so since the tag-archiving fix; GET /clients and GET /projects
// never did, and those two are what actually feed the client controls: the
// focus search in the sidebar, the Tasks quick-add select, the new-invoice
// select, the connector triage.
//
// The exclusion now lives in the service, so this spec pins the two halves
// of the contract that a unit test cannot see together:
//   1. an archived client is offered by NO picker in the SPA;
//   2. it is still reachable on the Clients page, which is the only door to
//      un-archive or purge it -- proving the include_archived opt-in really
//      travels to the server rather than being a client-side filter that
//      happens to hide the same rows.
//
// Locators are CSS classes and role names accepting both UI languages, as in
// the rest of the suite.

async function login(page: Page) {
  await page.goto('/login')
  await page.locator('input[type=email]').fill(EMAIL)
  await page.locator('input[type=password]').fill(PASSWORD)
  await page.locator('button[type=submit]').click()
  await page.waitForURL('**/notes', { timeout: 15_000 })
}

/** Drive the mode chip to a target mode (copied from app.spec.ts: archiving
 *  and un-archiving a client are owner-gated writes). */
async function ensureMode(page: Page, target: 'user' | 'owner' | 'admin') {
  const chip = page.locator('.modechip')
  await expect(chip).toBeVisible()
  await expect(async () => {
    const at = await chip.evaluate(
      (el, t) => el.classList.contains(`modechip--${t}`),
      target,
    )
    if (at) return
    await chip.click()
    throw new Error(`mode chip not at ${target} yet`)
  }).toPass({ timeout: 10_000 })
}

test('an archived client leaves every picker and stays on the Clients page', async ({
  page,
}) => {
  const stamp = Date.now()
  const live = `Vivo${stamp}`
  const dead = `Chiuso${stamp}`

  // Seeded through the API rather than clicked: this spec is about what the
  // pickers OFFER, and the two clients only need to exist.
  const a = await authedApi()
  const owner = { ...a.headers, 'X-Workspace-Role': 'owner' }
  const makeClient = async (name: string) => {
    const res = await a.ctx.post('/clients', {
      headers: owner,
      data: { name, legal_name: `${name} SRL` },
    })
    expect(res.status(), await res.text()).toBeLessThan(300)
    return (await res.json()) as { id: string; version: number }
  }

  // Every seeding call is INSIDE the try: a failure here still has rows to
  // clean up (and a context to dispose of), and putting them outside would
  // leak both on the day the seeding itself regresses.
  const seeded: { id: string }[] = []
  try {
    const liveRow = await makeClient(live)
    seeded.push(liveRow)
    const deadRow = await makeClient(dead)
    seeded.push(deadRow)
    // Archiving is the tag status soft-state, same door the Clients page uses.
    const pa = await a.ctx.patch(`/tags/${deadRow.id}`, {
      headers: owner,
      data: { expected_version: deadRow.version, status: 'archived' },
    })
    expect(pa.status(), await pa.text()).toBeLessThan(300)

    await login(page)

    // 1. The focus search (ClientSearch -> GET /clients?q=): typing the
    //    archived client's own name offers nothing, while its live sibling
    //    created in the same breath is offered normally. Searching for the
    //    shared stamp proves the row is missing from a result set the live
    //    one IS in -- not merely that the query matched nothing.
    const box = page.locator('.focus input.csearch__input')
    await box.click()
    await box.fill(String(stamp))
    await expect(page.locator('.csearch__row', { hasText: live })).toBeVisible({
      timeout: 10_000,
    })
    await expect(page.locator('.csearch__row', { hasText: dead })).toHaveCount(0)

    // 2. The Tasks quick-add client select (GET /clients, no search).
    await page.goto('/tasks')
    const select = page.locator('form.quickadd select').first()
    await expect(select.locator(`option:has-text("${live}")`)).toHaveCount(1, {
      timeout: 10_000,
    })
    await expect(select.locator(`option:has-text("${dead}")`)).toHaveCount(0)

    // 3. The Clients page. This is the step that proves the opt-in reaches
    //    the SERVER: the page fetches include_archived=true once and the
    //    checkbox is a local toggle over what came back, so if the endpoint
    //    ignored the parameter the archived row would never be in the
    //    payload and ticking the box would reveal nothing.
    await ensureMode(page, 'owner')
    await page.goto('/clients')
    const liveItem = page.locator('.cpitem', { hasText: live }).first()
    await expect(liveItem).toBeVisible({ timeout: 10_000 })
    await expect(page.locator('.cpitem', { hasText: dead })).toHaveCount(0)

    await page.locator('.cpcheck input[type=checkbox]').first().check()
    const deadItem = page.locator('.cpitem', { hasText: dead }).first()
    await expect(deadItem).toBeVisible({ timeout: 10_000 })

    // 4. And the round trip closes: un-archive from there and the client is
    //    a normal client again.
    await deadItem
      .getByRole('button', { name: /^unarchive$|^ripristina$/i })
      .click()
    await page.locator('.cpcheck input[type=checkbox]').first().uncheck()
    await expect(page.locator('.cpitem', { hasText: dead }).first()).toBeVisible({
      timeout: 10_000,
    })

    await expect(page.locator('main.content .err')).toHaveCount(0)
  } finally {
    // Leave the workspace as we found it: client tag names are unique per
    // org and the Clients page is name-ordered, so stray rows accumulate
    // across runs. Purge needs the client archived first (two-step).
    //
    // Wrapped: a cleanup that throws would REPLACE the assertion failure
    // that brought us here with a meaningless one, and hide the real
    // result of the run.
    try {
      const res = await a.ctx.get('/clients?include_archived=true', {
        headers: owner,
      })
      const body: unknown = res.ok() ? await res.json() : []
      const cur = Array.isArray(body)
        ? (body as { id: string; version: number }[])
        : []
      for (const row of seeded) {
        const it = cur.find((c) => c.id === row.id)
        if (!it) continue
        await a.ctx.patch(`/tags/${row.id}`, {
          headers: owner,
          data: { expected_version: it.version, status: 'archived' },
        })
        await a.ctx.delete(`/clients/${row.id}`, { headers: owner })
      }
    } catch {
      // Best effort: a leftover client is noise, a masked failure is a lie.
    }
    await a.ctx.dispose()
  }
})
