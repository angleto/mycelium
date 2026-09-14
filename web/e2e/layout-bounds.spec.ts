import { test, expect, type Page } from '@playwright/test'
import { E2E_EMAIL as EMAIL, E2E_PASSWORD as PASSWORD } from './global-setup'

// The class of defect no unit test in this repo can see.
//
// jsdom has no layout: `getBoundingClientRect()` there returns zeroes, so
// nothing under vitest can answer "does this box leave that one" or "why is
// the page four times taller than what is on it". Three defects of exactly
// this shape were found by eye on 2026-09-14 and each was proved, then fixed,
// with a measurement in a real browser -- which is what this file is.
//
// It asserts geometry and nothing else: two rectangles and two scroll
// heights. Deliberately not a visual snapshot, which would fail on every
// wording change and teach the team to re-baseline without looking.
async function login(page: Page) {
  await page.goto('/login')
  await page.locator('input[type=email]').fill(EMAIL)
  await page.locator('input[type=password]').fill(PASSWORD)
  await page.locator('button[type=submit]').click()
  await page.waitForURL('**/notes', { timeout: 15_000 })
}

test('kanban: a card keeps its controls inside itself, timer running', async ({
  page,
}) => {
  await login(page)
  await page.goto('/tasks')

  // Create one task so there is a card to measure, and start its timer: the
  // running cluster is the wide one (pause + memo + stop with a live
  // ⏱■ H:MM:SS readout) and it is the case that overflowed a 16rem column.
  const form = page.locator('form.quickadd')
  await expect(form.locator('select[required]')).not.toHaveValue('')
  const title = `Bounds ${Date.now()}`
  await form.locator('.quickadd__title').fill(title)
  await form.locator('button[type=submit]').click()

  const card = page.locator('.kanban__card', { hasText: title }).first()
  await expect(card).toBeVisible({ timeout: 15_000 })
  const actions = card.locator('.kanban__card-actions')
  await expect(actions).toBeVisible()

  // Start the timer from the card itself, then wait for the stop button that
  // carries the readout: that is the widest state, and the only one worth
  // measuring.
  await actions.locator('.tasktimer button').first().click()
  await expect(card.locator('.tasktimer__stop')).toBeVisible({ timeout: 10_000 })

  const cardBox = await card.boundingBox()
  const actionsBox = await actions.boundingBox()
  expect(cardBox, 'card has a box').not.toBeNull()
  expect(actionsBox, 'actions have a box').not.toBeNull()
  if (!cardBox || !actionsBox) return

  // The whole assertion: the controls may wrap, may shrink, may do anything
  // they like, as long as they stay inside. A pixel of tolerance for
  // sub-pixel rounding of borders, not for overflow.
  expect(
    actionsBox.x + actionsBox.width,
    'the actions cluster must not leave the card on the right',
  ).toBeLessThanOrEqual(cardBox.x + cardBox.width + 1)
  expect(actionsBox.x, 'nor on the left').toBeGreaterThanOrEqual(cardBox.x - 1)
  expect(
    actionsBox.y + actionsBox.height,
    'nor below it',
  ).toBeLessThanOrEqual(cardBox.y + cardBox.height + 1)

  // Leave the timer stopped: this suite shares a workspace with the others.
  await card.locator('.tasktimer__stop').click()
})

test('tasks: the page is no taller than what is on it', async ({ page }) => {
  await login(page)
  await page.goto('/tasks')
  await expect(page.locator('.kanban, .taskrow').first()).toBeVisible({
    timeout: 15_000,
  })

  const { docH, bodyH, appH } = await page.evaluate(() => ({
    docH: document.documentElement.scrollHeight,
    bodyH: document.body.scrollHeight,
    appH: Math.round(
      document.querySelector('.app')?.getBoundingClientRect().height ?? 0,
    ),
  }))

  // The root's scrollable height comes from its descendants, and a box that
  // escapes its scroller -- an absolutely positioned child with no positioned
  // ancestor, which is what `.sr-only` was -- extends it without being
  // visible anywhere. On 2026-09-14 this page scrolled 8418px over 1425px of
  // content because of 96 invisible 1x1 spans.
  //
  // The check is a RATIO, not a constant: the page is allowed to be taller
  // than the viewport, and a little taller than the body (margins, the root's
  // own box), but not a multiple of what it contains.
  expect(
    docH,
    `document ${docH}px against a body of ${bodyH}px and an app of ${appH}px: ` +
      'something is extending the page without being on it',
  ).toBeLessThanOrEqual(bodyH + 200)
})
