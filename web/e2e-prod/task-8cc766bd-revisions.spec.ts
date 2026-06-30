import { test, expect } from '@playwright/test'
import { req, patchTaskWithFreshVersion, E2E_TAG_ID } from './_api'

// Verify task 8cc766bd step 6: storia/diff/restore on SPA.
//   - sealed revision row expands into a diff table
//   - decimal/date/boolean/null render via the smart formatter
//     (no false-positive diffs on Decimal "12.50" vs 12.5)
//   - "Restore this field" reverts a single field
// Strategy: build the task + revisions via REST, then drive the SPA
// only for the rendering+restore behaviour.

interface TaskOut {
  id: string
  title: string
  version: number
  monetary_cost: string | null
  billable: boolean | null
}

test.describe('task 8cc766bd — revisions diff & restore', () => {
  let taskId = ''

  test.afterAll(async () => {
    if (!taskId) return
    const cur = await req<{ version: number }>(`/tasks/${taskId}`)
    if (cur.data?.version) {
      await req(`/tasks/${taskId}/delete`, {
        method: 'POST',
        body: { expected_version: cur.data.version },
      })
    }
  })

  test('revision row expands, formats values, restores single field', async ({
    page,
  }) => {
    // 1) Create the task via API. Title carries the timestamp so a
    //    rerun never collides; tag pins it for the cleanup sweep.
    const stamp = new Date().toISOString().replace(/[:.]/g, '-')
    const created = await req<TaskOut>('/tasks', {
      method: 'POST',
      body: {
        title: `e2e-claude task 8cc766bd ${stamp}`,
        description: 'initial body',
        importance: 3,
        urgency: 3,
        necessity: 'should',
        billable: true,
        monetary_cost: '12.50',
        due_date: '2026-06-05',
        tag_ids: [E2E_TAG_ID],
      },
    })
    taskId = created.data?.id ?? ''
    expect(created.status, JSON.stringify(created.data)).toBeLessThan(300)

    // 2) Two sealed revisions via API: each PATCH from a different
    //    channel without X-Edit-Session-Id is sealed-on-arrival, so
    //    two GET should list two sealed rows + the create baseline.
    //    Use the version-refreshing patch helper so the replica-lag
    //    retries don't produce a stale expected_version.
    const r1 = await patchTaskWithFreshVersion<TaskOut>(taskId, {
      title: `e2e-claude task 8cc766bd ${stamp} -- edited 1`,
    })
    expect(r1.status, `PATCH r1: ${JSON.stringify(r1.data)}`).toBe(200)
    const r2 = await patchTaskWithFreshVersion<TaskOut>(taskId, {
      monetary_cost: '99.00',
    })
    expect(r2.status).toBe(200)

    // 3) Open the task in the SPA. The detail route is /tasks/:id.
    await page.goto(`/tasks/${taskId}`)
    await page.waitForSelector('.revisions-panel', { timeout: 15_000 })

    // 4) Post-UX-fix the history panel ships collapsed; expand it via
    //    the toggle. Pre-fix builds had no toggle and rendered the
    //    list inline — guard the click so the spec stays green either
    //    way (prod often lags the local SPA changes by one deploy).
    const toggle = page.locator('.revisions-panel__toggle')
    if (await toggle.count()) await toggle.click()

    // 5) Three rows expected: create + edit1 (title) + edit2 (cost).
    //    edit2 is the most recent sealed -> first row in DESC order.
    const rows = page.locator('.revision-row')
    await expect(rows).toHaveCount(3)

    // 6) Expand the title-edit revision (nth(1) in DESC order). Its
    //    snapshot still has the OLD monetary_cost (12.50) because the
    //    cost edit happened later; current is 99.00. nth(0) is the
    //    cost-edit revision whose snapshot equals current (post-edit
    //    snapshot semantics) -> would render "no differences".
    await rows.nth(1).locator('.revision-meta-btn').click()
    const diff = page.locator('.revision-diff-table')
    await expect(diff).toBeVisible()

    // The "monetary_cost" row must appear in the diff and the
    // snapshot cell must render a number with 2 decimals in locale
    // form ("12,50" with the default it-IT or "12.50" with en-US).
    // The detail route renders in browser locale; we accept either.
    const costRow = diff.locator('tr', { hasText: /monetary_cost|Cost|Costo/i })
    await expect(costRow).toBeVisible()
    const snapCell = await costRow.locator('.revision-diff-snap').innerText()
    expect(snapCell).toMatch(/12[.,]50/)
    const currCell = await costRow.locator('.revision-diff-curr').innerText()
    expect(currCell).toMatch(/99([.,]00)?/)

    // 7) Smart rendering anti-false-positive: title was NOT touched
    //    in edit2 (only cost changed), so title must NOT appear in
    //    this diff even though Decimal/Date/boolean fields exist on
    //    the task. The diff length is exactly 1.
    const diffBodyRows = diff.locator('tbody tr')
    await expect(diffBodyRows).toHaveCount(1)

    // 8) "Restore this field": click on the cost row's restore
    //    button, confirm the browser confirm() dialog, and verify
    //    that monetary_cost goes back to 12.50.
    page.once('dialog', (d) => void d.accept())
    await costRow.locator('.revision-restore-field').click()

    // Wait for the API roundtrip to settle and the task to reload.
    await expect
      .poll(
        async () => {
          const r = await req<TaskOut>(`/tasks/${taskId}`)
          return r.data?.monetary_cost
        },
        { timeout: 10_000, intervals: [500, 1000] },
      )
      .toMatch(/12\.5/)
  })
})
