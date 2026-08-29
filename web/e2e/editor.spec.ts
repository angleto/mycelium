import { test, expect, type Page } from '@playwright/test'
import { E2E_EMAIL as EMAIL, E2E_PASSWORD as PASSWORD } from './global-setup'
import {
  placeCaret,
  readSource,
  setEditorMode,
  setSource,
  sourceContent,
} from './source-editor'

// Regression coverage for the note editor. There is ONE editing surface and
// ONE document -- the markdown source -- shown in either of two ways: the
// rendered view, and plain markdown. The toggle between them changes
// decorations and nothing else, so these tests assert the mode they need
// rather than inheriting a persisted preference from whatever ran before.
//
// The bugs pinned here reached production: the layout overlap, the
// <label>-forwarded double-click bold, the silent rewrite of a verbatim body
// on open, and the lost link destination.

async function login(page: Page) {
  await page.goto('/login')
  await page.locator('input[type=email]').fill(EMAIL)
  await page.locator('input[type=password]').fill(PASSWORD)
  await page.locator('button[type=submit]').click()
  await page.waitForURL('**/notes', { timeout: 15_000 })
}

async function openFreshNoteEditor(page: Page) {
  await page.getByRole('button', { name: 'New note' }).click()
  await page.locator('.modal__panel input').first().fill(`e2e editor ${Date.now()}`)
  await page.locator('.modal__foot button:not(.btn--ghost)').first().click()
  // The note page opens with the multi-part editor. New notes start with
  // zero parts (notes are split into markdown blocks), so add one to get a
  // rich-text body editor (.rte) to drive.
  await expect(page.locator('.parts-editor')).toBeVisible({ timeout: 10_000 })
  await page.getByRole('button', { name: 'Add part' }).click()
  await expect(page.locator('.rte').first()).toBeVisible({ timeout: 10_000 })
  // Give the autosave/remount a beat to settle before driving the editor.
  await page.waitForTimeout(1000)
}

/** Wait for the editing surface, and pin the view these specs assume. The
 *  mode preference is app-wide and persisted, so it outlives a spec. */
async function awaitSourceEditor(page: Page, mode: 'source' | 'visual' = 'visual') {
  await expect(sourceContent(page)).toBeVisible({ timeout: 10_000 })
  await setEditorMode(page, mode)
}

test('the editor is not wrapped in a <label> (double-click must not bold)', async ({
  page,
}) => {
  await login(page)
  await openFreshNoteEditor(page)
  // A <label> forwards user clicks to its first control (the Bold
  // button), so wrapping the editor made a double-click bold the word.
  const wrappedInLabel = await page.evaluate(
    () => !!document.querySelector('.rte')?.closest('label'),
  )
  expect(wrappedInLabel).toBe(false)
})

test('a markdown table previews as a table, and the source is untouched', async ({
  page,
}) => {
  await login(page)
  await openFreshNoteEditor(page)
  await awaitSourceEditor(page)
  // A trailing paragraph so the caret has somewhere to be that is NOT the
  // table: a block the selection touches shows its source, which is the whole
  // reveal rule.
  const md = ['| Name | Age |', '| --- | --- |', '| Alice | 30 |', '', 'dopo'].join('\n')
  await setSource(page, md)
  // Put the caret on that trailing paragraph EXPLICITLY. This used to click
  // the middle of the editor and trust it to land somewhere harmless, which
  // stopped being true when clicking a rendered block became the way to ask
  // for its source back: the click landed on the table, revealed it, and the
  // widget under test was gone.
  await placeCaret(page, md, md.length)
  // The table is PREVIEWED as a table -- a widget over the source, not a
  // second document model -- so there is nothing to serialise back.
  await expect(page.locator('.cm-md-table table').first()).toBeVisible()
  expect(await page.locator('.cm-md-table th').count()).toBe(2)
  expect(await readSource(page)).toBe(md)
})

// Markdown that is deliberately NOT a fixed point of the editor's
// round-trip: hard-wrapped prose, a padded table separator, links whose
// label is inline code, a relative path with a directory, bare brackets,
// and a trailing newline. Every one of these was silently rewritten when
// a note was merely OPENED (see the trailing-node / dirty-check chain in
// RichEditor).
const VERBATIM = [
  '# Titolo',
  '',
  'Un paragrafo avvolto a mano attorno alle 72 colonne,',
  'come in un file tenuto in repository.',
  '',
  '| File | Cosa copre |',
  '|------|------------|',
  '| [`00-overview.md`](00-overview.md) | stato [proven] |',
  '',
  '- [`sources/`](sources/): materiale originale',
  '',
].join('\n')

test('opening a note never rewrites a verbatim markdown part', async ({ page }) => {
  await login(page)
  await openFreshNoteEditor(page)
  await awaitSourceEditor(page)
  await setSource(page, VERBATIM)
  // Let the 1.2s debounced autosave land the bytes we typed.
  await page.waitForResponse(
    (r) => /\/notes\/[^/]+\/parts\/[^/]+$/.test(r.url()) && r.request().method() === 'PATCH',
    { timeout: 15_000 },
  )

  // From here on NOTHING may write. Re-opening the note mounts a fresh
  // editor over the stored body; that mount must not produce a PATCH.
  const writes: string[] = []
  page.on('request', (r) => {
    if (r.url().includes('/parts/') && ['PATCH', 'PUT', 'POST'].includes(r.method())) {
      writes.push(`${r.method()} ${r.url()}`)
    }
  })
  await page.reload()
  await expect(page.locator('.parts-editor')).toBeVisible({ timeout: 10_000 })
  await expect(sourceContent(page)).toBeVisible()
  // Opening the body must still write nothing. Well past the autosave
  // debounce.
  await page.waitForTimeout(3500)
  expect(writes).toEqual([])

  // There is no notice any more, because there is nothing to warn about:
  // the surface that would have normalised this body on the first edit is
  // gone, and the string the editor holds IS the string that was stored.
  await expect(page.locator('.rte__notice')).toHaveCount(0)

  // The source is still the bytes we uploaded, byte for byte.
  await awaitSourceEditor(page)
  expect(await readSource(page)).toBe(VERBATIM)
})

test('links keep their destination', async ({ page }) => {
  await login(page)
  await openFreshNoteEditor(page)
  await awaitSourceEditor(page)
  // Both arms used to LOSE the destination on the round trip: the first
  // because the `code` mark excluded `link`, the second because tiptap's
  // default isAllowedUri rejects a relative path containing a directory
  // separator. Neither can happen now -- there is no round trip -- so what
  // this asserts is the preview reading them as links and the source being
  // exactly what was typed.
  const md = '[`00-overview.md`](00-overview.md) e [testo](docs/00-overview.md)'
  await setSource(page, md)
  await page.locator('.cm-content').first().click()
  await page.keyboard.press('ControlOrMeta+ArrowDown')
  expect(await page.locator('.cm-md-linklabel').count()).toBe(2)
  expect(await readSource(page)).toBe(md)
})

test('sub/sup survive as the bytes the author wrote', async ({ page }) => {
  // The visual editor turned these into real <sub>/<sup> elements and
  // serialised them back. The editor no longer interprets them -- the READER
  // still does, which is where they are meant to be seen -- so what matters
  // here is that the source is untouched, code span included.
  await login(page)
  await openFreshNoteEditor(page)
  await awaitSourceEditor(page)
  const md = 'x<sub>0</sub> e 2<sup>t</sup>, ma `x<sup>2</sup>` resta letterale'
  await setSource(page, md)
  expect(await readSource(page)).toBe(md)
})

test('in markdown mode the Attach-file block does not overlap the editor', async ({
  page,
}) => {
  await login(page)
  await openFreshNoteEditor(page)
  await awaitSourceEditor(page)
  await setSource(page, Array.from({ length: 60 }, (_, i) => `line ${i + 1}`).join('\n'))
  const box = await page.evaluate(() => {
    const ta = document.querySelector('.rte__raw.rte__src')!.getBoundingClientRect()
    const btn = document
      .querySelector('.atts')!
      .querySelector('button')!
      .getBoundingClientRect()
    return { taBottom: ta.bottom, attachTop: btn.top }
  })
  // The attach button starts at or below the editor's bottom edge.
  expect(box.attachTop).toBeGreaterThanOrEqual(box.taBottom - 1)
})

test('the toolbar formats markdown SOURCE, and reports what is under the caret', async ({
  page,
}) => {
  // The toolbar used to be disabled in markdown mode: every button was a
  // document-model command and there was no model behind the source. They
  // are source transformations now, so the same buttons work everywhere.
  await login(page)
  await openFreshNoteEditor(page)
  await awaitSourceEditor(page)

  // By TITLE, not by accessible name: these buttons carry a glyph as their
  // content (`•`, `H1`), so the accessible name is the glyph and the label
  // lives in the tooltip.
  const btn = (title: string) => page.getByTitle(title, { exact: true }).first()

  await setSource(page, 'uno\ndue')
  await sourceContent(page).click()
  await page.keyboard.press('ControlOrMeta+a')
  await btn('Bullet list').click()
  expect(await readSource(page)).toBe('- uno\n- due')

  // Toggling back removes the markers rather than adding a second set.
  await sourceContent(page).click()
  await page.keyboard.press('ControlOrMeta+a')
  await btn('Bullet list').click()
  expect(await readSource(page)).toBe('uno\ndue')

  // A heading, and the pressed state that follows the caret into it.
  await sourceContent(page).click()
  await page.keyboard.press('ControlOrMeta+a')
  await btn('Heading 1').click()
  expect(await readSource(page)).toBe('# uno\n# due')
  await expect(btn('Heading 1')).toHaveClass(/rte__fmt--on/)
  await expect(btn('Heading 2')).not.toHaveClass(/rte__fmt--on/)
})

test('a markdown-source edit is what gets saved, byte for byte', async ({ page }) => {
  // The contract at the wire: the PATCH carries exactly the source, with
  // exactly the edit the toolbar made, and nothing else normalised.
  await login(page)
  await openFreshNoteEditor(page)
  await awaitSourceEditor(page)
  await setSource(page, VERBATIM)
  await page.waitForResponse(
    (r) => /\/notes\/[^/]+\/parts\/[^/]+$/.test(r.url()) && r.request().method() === 'PATCH',
    { timeout: 15_000 },
  )

  const bodies: string[] = []
  page.on('request', (r) => {
    if (r.url().includes('/parts/') && r.method() === 'PATCH') {
      const json = r.postDataJSON() as { body?: string } | null
      if (json?.body !== undefined) bodies.push(json.body)
    }
  })

  // Put the caret at a known offset and type one character.
  const at = VERBATIM.indexOf('paragrafo')
  await placeCaret(page, VERBATIM, at)
  await page.keyboard.type('X')

  await page.waitForResponse(
    (r) => /\/notes\/[^/]+\/parts\/[^/]+$/.test(r.url()) && r.request().method() === 'PATCH',
    { timeout: 15_000 },
  )
  await page.waitForTimeout(300)
  expect(bodies.at(-1)).toBe(VERBATIM.slice(0, at) + 'X' + VERBATIM.slice(at))
})

test('the @ typeahead inserts a mention link in markdown mode', async ({ page }) => {
  // The trigger has to match what the user TYPES (`@` + a title), not the
  // `@note:<uuid>` the completion produces -- a source keyed on the output
  // would never fire.
  await login(page)
  await openFreshNoteEditor(page)
  await awaitSourceEditor(page)

  await sourceContent(page).click()
  // The note this editor belongs to is itself a candidate, so its own title
  // is a query guaranteed to match.
  await page.keyboard.type('@e2e')
  await expect(page.locator('.cm-tooltip-autocomplete')).toBeVisible({ timeout: 15_000 })
  // CodeMirror refuses an accept within its interactionDelay (75ms) of the
  // popup opening, so a keystroke already in flight cannot pick an option the
  // user has not seen. Part of the contract, not a flake workaround.
  await page.waitForTimeout(200)
  await page.keyboard.press('Enter')

  const back = await readSource(page)
  expect(back).toMatch(/\]\(@(note|task):[0-9a-f-]{36}\) $/)
})
