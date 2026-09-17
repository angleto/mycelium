import { test, expect, type Page } from '@playwright/test'
import { E2E_EMAIL as EMAIL, E2E_PASSWORD as PASSWORD } from './global-setup'
import { authedApi } from './api'

// Possession (ADR-0063) on the BOARD, which is the half the unit tests
// cannot reach: they prove the badge renders from a possession, not that
// the board ever hands it one. The wiring is a request, a map and a
// lookup, and each of the three can be right while the card stays blank.
//
// It matters because of what a blank card means here. Ten sessions pull
// from the same column; a taken card that looks free is the collision
// this whole mechanism exists to prevent, discovered one round of wasted
// work later. The task page already said it -- but only to somebody who
// had opened that task, which is exactly the person who no longer needs
// telling.
//
// Seeded through the API rather than clicked into being: a lease is
// taken by a working session, and there is no way to open one from the
// interface (nor should there be -- it is what an agent session does at
// startup).

async function login(page: Page) {
  await page.goto('/login')
  await page.locator('input[type=email]').fill(EMAIL)
  await page.locator('input[type=password]').fill(PASSWORD)
  await page.locator('button[type=submit]').click()
  await page.waitForURL('**/notes', { timeout: 15_000 })
}

test('the board says which cards are taken, and by which session', async ({ page }) => {
  const api = await authedApi()
  const title = `possession-e2e-${Date.now()}`
  const freeTitle = `possession-e2e-free-${Date.now()}`
  // Unique per run, and not decoration: the e2e database is long
  // lived, a session stays open until somebody closes it, and two runs
  // sharing a label would leave the assertions pointing at whichever
  // row sorted first.
  const label = `verify-${Date.now()}`
  let taskId = ''
  let freeId = ''
  let freeVersion = 1
  let taskVersion = 1
  let workerId = ''
  try {
    const taskRes = await api.ctx.post('/tasks', {
      headers: api.headers,
      data: { title },
    })
    expect(
      taskRes.ok(),
      `create task failed: ${taskRes.status()} ${await taskRes.text()}`,
    ).toBeTruthy()
    const task = (await taskRes.json()) as { id: string; version: number }
    taskId = task.id
    taskVersion = task.version

    // A second card, deliberately unheld. The negative case needs a row
    // that is certainly free and certainly rendered: "some other row has
    // no badge" is an assertion about whatever else happens to be in the
    // workspace, which on a fresh database is nothing at all.
    const freeRes = await api.ctx.post('/tasks', {
      headers: api.headers,
      data: { title: freeTitle },
    })
    expect(
      freeRes.ok(),
      `create free task failed: ${freeRes.status()} ${await freeRes.text()}`,
    ).toBeTruthy()
    const freeTask = (await freeRes.json()) as { id: string; version: number }
    freeId = freeTask.id
    freeVersion = freeTask.version

    const workerRes = await api.ctx.post('/tasks/workers', {
      headers: api.headers,
      data: { label },
    })
    expect(
      workerRes.ok(),
      `open worker failed: ${workerRes.status()} ${await workerRes.text()}`,
    ).toBeTruthy()
    const worker = (await workerRes.json()) as { id: string }
    workerId = worker.id

    const leaseRes = await api.ctx.post(`/tasks/${task.id}/leases`, {
      headers: api.headers,
      data: { worker_id: worker.id },
    })
    expect(
      leaseRes.ok(),
      `acquire lease failed: ${leaseRes.status()} ${await leaseRes.text()}`,
    ).toBeTruthy()

    await login(page)
    await page.goto('/tasks')

    // The kanban is what the board opens on, so it is what is asserted
    // first. The NAME travels, not the id: nobody reading a board
    // resolves a uuid, which is why the server resolves it into the
    // projection rather than making every client ask.
    const card = page.locator('.kanban__card', { hasText: title }).first()
    await expect(card).toBeVisible({ timeout: 15_000 })
    await expect(card.locator('.leasebadge')).toContainText(label)

    // And the list, which is a second component reading the same map: a
    // badge wired into one view and not the other is the likelier
    // mistake, not a badge that renders nowhere.
    await page.getByRole('tab', { name: /^(list|lista)$/i }).click()
    const row = page.locator('li.taskrow', { hasText: title }).first()
    await expect(row).toBeVisible({ timeout: 15_000 })
    await expect(row.locator('.leasebadge')).toContainText(label)

    // And the sessions running, with what each one holds. Nothing in the
    // app showed this before: the possession of one task was visible on
    // that task, so "how many agents are running, and on what" had
    // nowhere to be asked.
    const sessions = page.locator('details.sessions')
    await expect(sessions).toBeVisible()
    await sessions.locator('summary').click()
    await expect(sessions.locator('.sessions__row', { hasText: label })).toContainText(title)

    // A card nobody holds carries no badge. Asserted on the same page as
    // the positive case, because a badge that rendered on every row would
    // satisfy the assertions above and say nothing.
    const freeRow = page.locator('li.taskrow', { hasText: freeTitle }).first()
    await expect(freeRow).toBeVisible()
    await expect(freeRow.locator('.leasebadge')).toHaveCount(0)
  } finally {
    // Closing the session gives back everything it holds, which is the
    // fast half of recovery and also the tidy-up this spec owes: an open
    // worker outlives the run otherwise.
    if (workerId) {
      await api.ctx.post(`/tasks/workers/${workerId}/close`, {
        headers: api.headers,
        data: {},
      })
    }
    if (taskId) {
      await api.ctx.post(`/tasks/${taskId}/delete`, {
        headers: api.headers,
        data: { expected_version: taskVersion },
      })
    }
    if (freeId) {
      await api.ctx.post(`/tasks/${freeId}/delete`, {
        headers: api.headers,
        data: { expected_version: freeVersion },
      })
    }
    await api.ctx.dispose()
  }
})
