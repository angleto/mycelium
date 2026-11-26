import { test, expect, type Page } from '@playwright/test'
import { authedApi } from './api'
import { E2E_EMAIL as EMAIL, E2E_PASSWORD as PASSWORD } from './global-setup'

// A session that needs the person is found from anywhere in the app, and
// reads as what it is (docs/adr/0078).
//
// What this replaced was a collapsed list of worker rows on the tasks board
// that could not say which session was waiting. The question the page exists
// for is asked in a browser, from another page: is anything waiting for me,
// and which one. The session is opened and reported the way an agent host's
// hooks do it, with an agent token, because a person's browser cannot.
//
// The e2e database is long-lived and shared by every spec, so the session is
// found by a project name unique to this run, never by position or count.

async function login(page: Page) {
  await page.goto('/login')
  await page.locator('input[type=email]').fill(EMAIL)
  await page.locator('input[type=password]').fill(PASSWORD)
  await page.locator('button[type=submit]').click()
  await page.waitForURL('**/notes', { timeout: 15_000 })
}

interface Host {
  report: (attention: string, blockedOn?: string) => Promise<void>
  close: () => Promise<void>
}

async function hostSession(project: string): Promise<Host> {
  // Minting a credential is an owner's act, taken explicitly (roles are sudo-style).
  const { ctx, headers } = await authedApi({ 'X-Workspace-Role': 'owner' })
  const minted = await ctx.post('/agent-tokens', {
    headers,
    data: { name: `e2e-hooks-${project}`, scope: 'cli' },
  })
  expect(minted.ok(), await minted.text()).toBeTruthy()
  const raw = ((await minted.json()) as { raw: string }).raw
  const hooks = {
    Authorization: `Bearer ${raw}`,
    'X-Workspace-Id': headers['X-Workspace-Id'],
    'Content-Type': 'application/json',
  }
  const opened = await ctx.post('/sessions/open', {
    headers: hooks,
    data: { agent: 'claude_code', host_ref: `e2e-${project}`, project, machine: 'e2e-box' },
  })
  expect(opened.ok(), await opened.text()).toBeTruthy()
  const id = ((await opened.json()) as { session: { id: string } }).session.id
  return {
    report: async (attention, blockedOn) => {
      const r = await ctx.post('/sessions/report', {
        headers: hooks,
        data: { session_id: id, attention, blocked_on: blockedOn ?? null },
      })
      expect(r.ok(), await r.text()).toBeTruthy()
    },
    close: async () => {
      await ctx.post('/sessions/close', { headers: hooks, data: { session_id: id } })
      await ctx.dispose()
    },
  }
}

test('a session waiting for a permission is found from any page and read in words', async ({
  page,
}) => {
  const project = `e2e-proj-${Date.now()}`
  const host = await hostSession(project)
  try {
    await host.report('blocked', 'permission')
    await login(page)

    // From the notes page: the shell says something is waiting.
    const badge = page.getByRole('link', { name: /(waiting for you|aspett)/i })
    await expect(badge).toBeVisible({ timeout: 20_000 })
    await badge.click()
    await page.waitForURL('**/sessions')

    const waiting = page.getByRole('region', { name: /^(Waiting for you|Aspettano te)/ })
    const row = waiting.getByRole('listitem').filter({ hasText: project })
    await expect(row).toBeVisible()
    await expect(row).toContainText(/(Waiting for a permission|Aspetta un permesso)/)
    await expect(row).toContainText('e2e-box')

    // The session finishes its turn: still waiting for the person, in other
    // words. The page reads it again on its own, without a reload.
    await host.report('your_turn')
    await expect(row).toContainText(/(Finished, waiting for your next instruction|Ha finito)/, {
      timeout: 30_000,
    })

    // The board keeps one line that leads here.
    await page.goto('/tasks')
    // In the page itself, not the shell's badge, which says the same.
    const line = page
      .getByRole('main')
      .getByRole('link', { name: /^\d+ (sessions?|sessioni?) · /i })
    await expect(line).toBeVisible()
    await line.click()
    await page.waitForURL('**/sessions')
  } finally {
    await host.close()
  }
})
