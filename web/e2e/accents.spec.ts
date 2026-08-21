import { test, expect, type Page } from '@playwright/test'
import { E2E_EMAIL as EMAIL, E2E_PASSWORD as PASSWORD } from './global-setup'
import { authedApi } from './api'
import { readSource, sourceContent } from './source-editor'

// Accented letters typed the way macOS types them: a DEAD KEY. Option+`
// then `e` is not two characters, it is one IME composition — the browser
// fires compositionstart / compositionupdate("`") / compositionend("è")
// and ProseMirror must be left alone for its DOMObserver to read the
// result. Anything that dispatches a transaction, or rebuilds the
// document, inside that window kills the composition and the letter is
// lost. Italian is unwriteable when that happens.

async function login(page: Page) {
  await page.goto('/login')
  await page.locator('input[type=email]').fill(EMAIL)
  await page.locator('input[type=password]').fill(PASSWORD)
  await page.locator('button[type=submit]').click()
  await page.waitForURL('**/notes', { timeout: 15_000 })
}

async function openFreshNoteEditor(page: Page) {
  await page.getByRole('button', { name: 'New note' }).click()
  await page.locator('.modal__panel input').first().fill(`e2e accents ${Date.now()}`)
  await page.locator('.modal__foot button:not(.btn--ghost)').first().click()
  await expect(page.locator('.parts-editor')).toBeVisible({ timeout: 10_000 })
  await page.getByRole('button', { name: 'Add part' }).click()
  await expect(page.locator('.rte').first()).toBeVisible({ timeout: 10_000 })
  await page.waitForTimeout(1000)
}

// One dead-key accent, as macOS + Chrome deliver it. The keydowns matter
// as much as the composition: the dead key arrives as `key: "Dead"` with
// Option held and keyCode 229, and the letter that commits it arrives as
// another 229 keydown. A handler that swallows either one breaks the
// accent without ever touching the composition machinery.
async function typeDeadKeyAccent(
  page: Page,
  pending: string,
  composed: string,
  pauseMs = 120,
) {
  const cdp = await page.context().newCDPSession(page)
  // Option + `  -> dead key, composition opens holding the bare accent.
  await cdp.send('Input.dispatchKeyEvent', {
    type: 'rawKeyDown',
    key: 'Dead',
    code: 'Backquote',
    modifiers: 1, // Alt
    windowsVirtualKeyCode: 229,
    nativeVirtualKeyCode: 229,
  })
  await cdp.send('Input.imeSetComposition', {
    text: pending,
    selectionStart: pending.length,
    selectionEnd: pending.length,
  })
  await cdp.send('Input.dispatchKeyEvent', {
    type: 'keyUp',
    key: 'Dead',
    code: 'Backquote',
    modifiers: 1,
    windowsVirtualKeyCode: 229,
    nativeVirtualKeyCode: 229,
  })
  await page.waitForTimeout(pauseMs)
  // The letter: another 229 keydown, then the composition commits.
  await cdp.send('Input.dispatchKeyEvent', {
    type: 'rawKeyDown',
    key: composed,
    code: 'KeyE',
    windowsVirtualKeyCode: 229,
    nativeVirtualKeyCode: 229,
  })
  await cdp.send('Input.insertText', { text: composed })
  await cdp.send('Input.dispatchKeyEvent', {
    type: 'keyUp',
    key: composed,
    code: 'KeyE',
    windowsVirtualKeyCode: 229,
    nativeVirtualKeyCode: 229,
  })
  await cdp.detach()
}

// Same, but the composition is left PENDING for a while before it is
// committed — which is what actually happens when a human presses the
// dead key, reads the screen, and then presses the letter. The 1.2s
// autosave debounce fits inside that pause.
const typeDeadKeyAccentSlowly = (
  page: Page,
  pending: string,
  composed: string,
  pauseMs: number,
) => typeDeadKeyAccent(page, pending, composed, pauseMs)

test('dead-key accents survive in the rich editor', async ({ page }) => {
  await login(page)
  await openFreshNoteEditor(page)

  const pm = page.locator('.ProseMirror').first()
  await expect(pm).toBeVisible()
  await pm.click()
  await page.keyboard.type('citt')
  await typeDeadKeyAccent(page, '`', 'à')
  await page.keyboard.type(' perch')
  await typeDeadKeyAccent(page, '`', 'è')

  await expect(pm).toHaveText(/città perchè/)
})

test('a dead-key accent survives the autosave landing mid-composition', async ({
  page,
}) => {
  await login(page)
  await openFreshNoteEditor(page)

  const pm = page.locator('.ProseMirror').first()
  await pm.click()
  await page.keyboard.type('citt')
  // Hold the composition open across the 1.2s autosave debounce, so the
  // PATCH (and whatever the parent does with its response) lands while
  // the accent is still pending.
  await typeDeadKeyAccentSlowly(page, '`', 'à', 2500)

  await expect(pm).toHaveText(/città/)
})

test('a dead-key accent survives typing after an autosave round-trip', async ({
  page,
}) => {
  await login(page)
  await openFreshNoteEditor(page)

  const pm = page.locator('.ProseMirror').first()
  await pm.click()
  await page.keyboard.type('citt')
  // Let the debounced PATCH land and the parent adopt whatever comes back.
  await page.waitForResponse(
    (r) => /\/notes\/[^/]+\/parts\/[^/]+$/.test(r.url()) && r.request().method() === 'PATCH',
    { timeout: 15_000 },
  )
  await page.waitForTimeout(500)
  await typeDeadKeyAccent(page, '`', 'à')

  await expect(pm).toHaveText(/città/)
})

test('a dead-key accent survives an entity-prefix lookup resolving mid-composition', async ({
  page,
}) => {
  await login(page)
  await openFreshNoteEditor(page)

  const pm = page.locator('.ProseMirror').first()
  await pm.click()
  // A backticked hex prefix makes EntityPrefix fire an async lookup whose
  // resolution dispatches a transaction into the editor.
  await page.keyboard.type('vedi `91cf6aaa` citt')
  await typeDeadKeyAccentSlowly(page, '`', 'à', 1500)

  await expect(pm).toHaveText(/città/)
})

test('an accented word survives the save round-trip', async ({ page }) => {
  await login(page)
  await openFreshNoteEditor(page)

  const pm = page.locator('.ProseMirror').first()
  await pm.click()
  await page.keyboard.type('perch')
  await typeDeadKeyAccent(page, '`', 'è')
  await page.keyboard.type(' la citt')
  await typeDeadKeyAccent(page, '`', 'à')
  await page.keyboard.type(' è così: più accenti, é acuto, ò grave.')

  await page.waitForResponse(
    (r) => /\/notes\/[^/]+\/parts\/[^/]+$/.test(r.url()) && r.request().method() === 'PATCH',
    { timeout: 15_000 },
  )
  await page.waitForTimeout(800)
  await page.reload()
  await expect(page.locator('.parts-editor')).toBeVisible({ timeout: 10_000 })

  await expect(page.locator('.ProseMirror').first()).toHaveText(
    /perchè la città è così: più accenti, é acuto, ò grave\./,
  )
})

// Seed a note whose single part is LARGE and decoration-heavy: every
// transaction rebuilds the annotation decorations over the whole document
// and EntityPrefix rescans it for backticked ids, so a keystroke on a real
// note costs far more than on the four-word ones above. If a keystroke gets
// slow enough, the browser drops the pending composition and the accent is
// simply lost — which is what a big note in daily use looks like.
async function createBigNote(): Promise<string> {
  const { ctx, headers } = await authedApi()
  const note = await (
    await ctx.post('/notes', {
      headers,
      data: { kind: 'text', title: `e2e accents big ${Date.now()}` },
    })
  ).json()
  const para = (i: number) =>
    `## Sezione ${i}\n\nVedi \`91cf6aaa\` e \`4836a6cc\` per il contesto; ` +
    'la città è più a nord, perché il perimetro non è ancora definito. ' +
    'Un paragrafo di riempimento ragionevolmente lungo, come in una nota vera.\n'
  const body = Array.from({ length: 400 }, (_, i) => para(i)).join('\n')
  await ctx.post(`/notes/${note.id}/parts`, { headers, data: { body } })
  await ctx.dispose()
  return note.id as string
}

test('a dead-key accent survives on a large, decoration-heavy note', async ({
  page,
}) => {
  const noteId = await createBigNote()
  await login(page)
  await page.goto(`/notes/${noteId}`)

  const pm = page.locator('.ProseMirror').first()
  await expect(pm).toBeVisible({ timeout: 15_000 })
  await expect(pm).toContainText('Sezione 0', { timeout: 15_000 })

  // Put the caret at the very end and type there.
  await pm.click()
  await page.keyboard.press('ControlOrMeta+End')
  await page.keyboard.type(' la citt')
  await typeDeadKeyAccent(page, '`', 'à')

  await expect(pm).toContainText('la città', { timeout: 15_000 })
})

test('dead-key accents survive in markdown mode', async ({ page }) => {
  await login(page)
  await openFreshNoteEditor(page)

  await page.getByRole('button', { name: /Edit as Markdown/ }).first().click()
  const src = sourceContent(page)
  await expect(src).toBeVisible()
  await src.click()
  await page.keyboard.type('citt')
  await typeDeadKeyAccentSlowly(page, '`', 'à', 2500)

  expect(await readSource(page)).toBe('città')
})

test('dead-key accents survive in the note title field', async ({ page }) => {
  await login(page)
  await openFreshNoteEditor(page)

  const title = page.locator('.parts-editor input[type=text]').first()
  await title.click()
  await title.fill('')
  await page.keyboard.type('citt')
  await typeDeadKeyAccentSlowly(page, '`', 'à', 2500)

  expect(await title.inputValue()).toBe('città')
})
