import { test, expect, type Page } from '@playwright/test'
import { E2E_EMAIL as EMAIL, E2E_PASSWORD as PASSWORD } from './global-setup'

// The Save button of the time-entry edit form must be REACHABLE, not
// merely present in the DOM. It regressed by being present and
// unreachable: the form was rendered inside .taskrow__title, a span that
// is single-line and clipped on purpose (white-space: nowrap +
// overflow: hidden keeps a long title off the row's action buttons). A
// <select> is as wide as its longest OPTION, so a long task title made
// the form 912px wide inside an 800px span, and the 112px that were cut
// off carried Save and Close — the date and memo fields stayed visible,
// so the row looked editable and could not be saved.
//
// toBeVisible() does not catch this: a clipped element still has a
// non-empty box and visibility: visible. So the assertion here is the
// property that was actually violated — the point where Save is painted
// hit-tests to Save — plus Playwright's own actionability check, which
// covers being covered by something else as well.
//
// The long title is the trigger, so the fixture carries one. Both the
// entry and the task are removed in a finally: this spec runs against a
// live stack and must not leave billable time behind.

const LONG_TITLE =
  'BC2X on MySQL changes the transaction isolation of the ENTIRE server, and without SUPER it will not connect'

async function login(page: Page) {
  await page.goto('/login')
  await page.locator('input[type=email]').fill(EMAIL)
  await page.locator('input[type=password]').fill(PASSWORD)
  await page.locator('button[type=submit]').click()
  await page.waitForURL('**/notes', { timeout: 15_000 })
}

test('the Save button of a time entry is reachable however long the task title is', async ({
  page,
}) => {
  await login(page)
  const raw = await page.evaluate(() => localStorage.getItem('mycelium.session'))
  if (!raw) throw new Error('no session in localStorage after login')
  const { token, workspaceId } = JSON.parse(raw) as {
    token: string
    workspaceId: string
  }
  const headers = {
    Authorization: `Bearer ${token}`,
    'x-workspace-id': workspaceId,
    'content-type': 'application/json',
  }

  const stamp = Date.now()
  const title = `${LONG_TITLE} ${stamp}`
  const taskRes = await page.request.post('/api/tasks', {
    headers,
    data: { title, executor_kind: 'llm_agent', necessity: 'should' },
  })
  expect(
    taskRes.ok(),
    `create task failed: ${taskRes.status()} ${await taskRes.text()}`,
  ).toBeTruthy()
  const task = (await taskRes.json()) as { id: string; version: number }

  // Inside the default period of the Time view (the current month), and
  // closed, so the row renders as an editable past entry rather than a
  // running timer.
  const ended = new Date(stamp - 30 * 60_000)
  const started = new Date(stamp - 60 * 60_000)
  const entryRes = await page.request.post('/api/time/entries', {
    headers,
    data: {
      task_id: task.id,
      started_at: started.toISOString(),
      ended_at: ended.toISOString(),
    },
  })
  expect(
    entryRes.ok(),
    `create entry failed: ${entryRes.status()} ${await entryRes.text()}`,
  ).toBeTruthy()
  const entry = (await entryRes.json()) as { id: string; version: number }

  try {
    await page.goto('/time')
    const row = page.locator('li.taskrow', { hasText: title }).first()
    await expect(row).toBeVisible({ timeout: 15_000 })

    await row.getByRole('button', { name: /edit entry|modifica voce/i }).click()

    const save = row.getByRole('button', { name: /^(save|salva)$/i })
    await expect(save).toBeVisible()
    await expect(save).toBeEnabled()

    // Actionability: scrolls into view, waits for stability, and refuses
    // to act if another element would receive the click. `trial` runs
    // every one of those checks and performs no click.
    await save.click({ trial: true })

    // The geometric invariant, stated directly so a future regression
    // fails on the reason rather than on a Playwright timeout: whatever
    // is painted at the centre of Save must BE Save. An element clipped
    // away by an ancestor's overflow keeps its box and fails here.
    const hitsItself = await save.evaluate((el) => {
      const r = el.getBoundingClientRect()
      const hit = document.elementFromPoint(
        r.left + r.width / 2,
        r.top + r.height / 2,
      )
      return hit === el || el.contains(hit)
    })
    expect(
      hitsItself,
      'Save is painted where it cannot be clicked (clipped by an ancestor)',
    ).toBe(true)
  } finally {
    await page.request.delete(`/api/time/entries/${entry.id}`, { headers })
    await page.request.post(`/api/tasks/${task.id}/delete`, {
      headers,
      data: { expected_version: task.version },
    })
  }
})
