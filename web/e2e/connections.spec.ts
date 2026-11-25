import { test, expect, type Page } from '@playwright/test'
import { authedApi } from './api'
import { E2E_EMAIL as EMAIL, E2E_PASSWORD as PASSWORD } from './global-setup'

// Connecting two notes from the note page, and seeing it from the other one
// (ADR-0077).
//
// This is the act the connections panel was built for: a person who wanted
// to connect two notes could not find where to do it. Asserted in a browser
// because what failed was the path a person takes (which tab, which button,
// which words), and because the direction is only right if each of the two
// pages says it from its own side: "Comes from B" on A, "Gave rise to A" on
// B. A unit test of either page would pass with the two sides swapped.

async function login(page: Page) {
  await page.goto('/login')
  await page.locator('input[type=email]').fill(EMAIL)
  await page.locator('input[type=password]').fill(PASSWORD)
  await page.locator('button[type=submit]').click()
  await page.waitForURL('**/notes', { timeout: 15_000 })
}

async function createNote(title: string): Promise<string> {
  const { ctx, headers } = await authedApi()
  const note = (await (await ctx.post('/notes', { headers, data: { kind: 'text', title } })).json()) as {
    id: string
  }
  await ctx.dispose()
  return note.id
}

test('two notes are connected from one page, read from both, and disconnected', async ({ page }) => {
  const stamp = Date.now()
  const a = await createNote(`conn A ${stamp}`)
  const b = await createNote(`conn B ${stamp}`)
  await login(page)
  // The interface language follows the browser; the test reads both.
  await page.goto(`/notes/${a}`)
  const panel = page.locator('.connections')
  await expect(panel).toBeVisible()

  await panel.locator('.connections__add select').selectOption('derives_from:out:note')
  await panel.locator('.taskpicklist__search').fill(`conn B ${stamp}`)
  await panel.locator('.taskpicklist__item', { hasText: `conn B ${stamp}` }).click()

  const groupOnA = panel.locator('.connections__group', { hasText: `conn B ${stamp}` })
  await expect(groupOnA.locator('.connections__grouphead')).toHaveText(/^(Comes from|Nasce da)$/)

  await page.goto(`/notes/${b}`)
  const groupOnB = page.locator('.connections__group', { hasText: `conn A ${stamp}` })
  await expect(groupOnB.locator('.connections__grouphead')).toHaveText(/^(Gave rise to|Da qui è nato)$/)

  await groupOnB.getByRole('button', { name: /^(Disconnect|Scollega)$/ }).click()
  await expect(page.locator('.connections__group', { hasText: `conn A ${stamp}` })).toHaveCount(0)
  await page.goto(`/notes/${a}`)
  await expect(page.locator('.connections')).toBeVisible()
  await expect(page.locator('.connections__group', { hasText: `conn B ${stamp}` })).toHaveCount(0)
})
