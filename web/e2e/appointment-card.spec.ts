import { test, expect, type Page } from '@playwright/test'
import { E2E_EMAIL as EMAIL, E2E_PASSWORD as PASSWORD } from './global-setup'

// Task 2e43ae29: appointment tasks (start_at + duration_minutes) must
// show a date badge on the KANBAN card. The list view already rendered
// the appointment badge; the board card only showed due_date, so an
// appointment with no due_date showed no date at all. This drives the
// real stack: create an appointment task via the API (the field combo
// that makes a task an appointment), then assert its kanban card shows
// the clock start badge with the duration. Asserts on the glyph +
// duration (locale/timezone-independent), not the formatted date text.
// The task is deleted in a finally so the appointment never blocks a
// later run via the no-overlap invariant and the dev DB stays tidy.

async function login(page: Page) {
  await page.goto('/login')
  await page.locator('input[type=email]').fill(EMAIL)
  await page.locator('input[type=password]').fill(PASSWORD)
  await page.locator('button[type=submit]').click()
  await page.waitForURL('**/notes', { timeout: 15_000 })
}

test('kanban card shows the appointment badge for an appointment task', async ({
  page,
}) => {
  await login(page)
  const raw = await page.evaluate(() => localStorage.getItem('flow.session'))
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
  const title = `Appt card ${stamp}`
  // A fixed far-future slot: the no-overlap invariant rejects a second
  // appointment for the same participant in an overlapping window, so
  // anchoring far from any real (or leftover) appointment keeps the
  // create deterministic. The finally deletes it, so repeated runs never
  // overlap each other either.
  const start = new Date(Date.UTC(2099, 5, 15, 12, 0, 0))
  const res = await page.request.post('/api/tasks', {
    headers,
    data: {
      title,
      executor_kind: 'human',
      necessity: 'should',
      start_at: start.toISOString(),
      duration_minutes: 60,
    },
  })
  expect(
    res.ok(),
    `create failed: ${res.status()} ${await res.text()}`,
  ).toBeTruthy()
  const created = (await res.json()) as { id: string; version: number }

  try {
    // Land on /tasks in the kanban view (default on desktop; force it so
    // the assertion is viewport-independent).
    await page.evaluate(() => localStorage.setItem('flow.tasks.view', 'kanban'))
    await page.goto('/tasks')
    await expect(page.locator('.kanban')).toBeVisible({ timeout: 10_000 })

    const card = page.locator('.kanban__card', { hasText: title }).first()
    await expect(card).toBeVisible({ timeout: 10_000 })
    // The appointment badge: clock glyph + the duration, distinct from
    // the 📅 due-date badge the card used to show exclusively.
    const badge = card.locator('.kanban__meta .muted', { hasText: '🕒' })
    await expect(badge).toBeVisible()
    await expect(badge).toContainText('60m')
  } finally {
    await page.request.post(`/api/tasks/${created.id}/delete`, {
      headers,
      data: { expected_version: created.version },
    })
  }
})
