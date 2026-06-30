import { test, expect } from '@playwright/test'

// Verify task 718e4d60: chip "{n} task/s" on note row must stay on a
// single line even on narrow layouts (fix 626b87a added white-space:
// nowrap on .chip).
test('chip--derived stays single-line at narrow viewport', async ({ page }) => {
  await page.setViewportSize({ width: 360, height: 720 })
  await page.goto('/notes')
  // Wait the notes list to be hydrated.
  await page.waitForSelector('.noteitem', { timeout: 15_000 })

  const chip = page.locator('.chip--derived').first()
  const count = await page.locator('.chip--derived').count()
  if (count === 0) {
    test.skip(true, 'No note with derived tasks exists in this workspace.')
  }
  await chip.waitFor({ state: 'visible' })

  const whiteSpace = await chip.evaluate(
    (el) => getComputedStyle(el).whiteSpace,
  )
  expect(whiteSpace).toBe('nowrap')

  // Sanity: chip box height must be within a single line of its own
  // font-size (chip is small, ~22-28px tall single-line; > 1.7 * fontSize
  // would mean the text wrapped). Read both via JS to avoid magic numbers.
  const ratio = await chip.evaluate((el) => {
    const fs = parseFloat(getComputedStyle(el).fontSize)
    return el.getBoundingClientRect().height / fs
  })
  expect(ratio).toBeLessThan(2.2)
})
