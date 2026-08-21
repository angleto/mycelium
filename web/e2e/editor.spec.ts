import { test, expect, type Page } from '@playwright/test'
import { E2E_EMAIL as EMAIL, E2E_PASSWORD as PASSWORD } from './global-setup'
import { inSourceMode, readSource, setSource, sourceContent } from './source-editor'

// Regression coverage for the note rich editor (RichEditor + tiptap):
// the layout overlap, the <label>-forwarded double-click bold, and the
// GFM table round-trip — all bugs that reached production.

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

// The WYSIWYG<->Markdown mode toggle lives in the collapsible toolbar;
// its label flips between "Edit as Markdown" and "Rich editor".
const toggleBtn = (page: Page) =>
  page.getByRole('button', { name: /Edit as Markdown|Rich editor/ }).first()

// The toolbar tools (incl. the mode toggle) collapse behind the "Aa"
// button per a saved preference; expand them if the toggle is hidden.
async function ensureToolbar(page: Page) {
  if (!(await toggleBtn(page).isVisible().catch(() => false))) {
    await page.locator('.rte__collapse').first().click()
    await expect(toggleBtn(page)).toBeVisible()
  }
}

async function enterMarkdownMode(page: Page) {
  await ensureToolbar(page)
  if (!(await inSourceMode(page))) {
    await toggleBtn(page).click()
    await page.waitForTimeout(300)
  }
  await expect(sourceContent(page)).toBeVisible()
}

test('rich editor is not wrapped in a <label> (double-click must not bold)', async ({
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

test('markdown table round-trips through the editor', async ({ page }) => {
  await login(page)
  await openFreshNoteEditor(page)
  await enterMarkdownMode(page)
  const md = ['| Name | Age |', '| --- | --- |', '| Alice | 30 |'].join('\n')
  await setSource(page, md)
  await toggleBtn(page).click() // -> WYSIWYG
  // WYSIWYG renders a real table.
  await expect(page.locator('.ProseMirror table').first()).toBeVisible()
  expect(await page.locator('.ProseMirror th').count()).toBe(2)
  // Back to markdown: serializes to a pipe table again.
  await toggleBtn(page).click()
  await expect(sourceContent(page)).toBeVisible()
  const back = await readSource(page)
  expect(back).toContain('| Name')
  expect(back).toContain('| Alice')
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
  await enterMarkdownMode(page)
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
  // The rich editor is the default view for every body, verbatim ones
  // included: rendering is a mode, not a property of how the bytes arrived.
  await expect(page.locator('.ProseMirror').first()).toBeVisible()
  await expect(page.locator('.rte__src')).toHaveCount(0)
  // Rendering that body must still write nothing. Well past the autosave
  // debounce.
  await page.waitForTimeout(3500)
  expect(writes).toEqual([])

  // The body is not a fixed point of the round-trip, so an edit made HERE
  // would normalise it. Said once, in a notice; it withholds nothing.
  await expect(page.locator('.rte__notice').first()).toBeVisible()

  // And the source is still the bytes we uploaded, byte for byte.
  await enterMarkdownMode(page)
  expect(await readSource(page)).toBe(VERBATIM)
})

test('links keep their destination through the rich editor', async ({ page }) => {
  await login(page)
  await openFreshNoteEditor(page)
  await enterMarkdownMode(page)
  // Both arms used to lose the destination: the first because the `code`
  // mark excluded `link`, the second because tiptap's default isAllowedUri
  // rejects a relative path containing a directory separator.
  const md = '[`00-overview.md`](00-overview.md) e [testo](docs/00-overview.md)'
  await setSource(page, md)
  await toggleBtn(page).click() // -> WYSIWYG
  await expect(page.locator('.ProseMirror a').first()).toBeVisible()
  expect(await page.locator('.ProseMirror a').count()).toBe(2)
  await toggleBtn(page).click() // -> back to markdown
  expect(await readSource(page)).toBe(md)
})

test('sub/sup render as real elements and round-trip, but stay literal in code', async ({
  page,
}) => {
  await login(page)
  await openFreshNoteEditor(page)
  await enterMarkdownMode(page)
  const md = 'x<sub>0</sub> e 2<sup>t</sup>, ma `x<sup>2</sup>` resta letterale'
  await setSource(page, md)
  await toggleBtn(page).click() // -> WYSIWYG
  await expect(page.locator('.ProseMirror sup').first()).toBeVisible()
  expect(await page.locator('.ProseMirror sup').count()).toBe(1)
  expect(await page.locator('.ProseMirror sub').count()).toBe(1)
  // Inside a code span the author asked for the characters, not the markup.
  expect(await page.locator('.ProseMirror code sup').count()).toBe(0)
  await toggleBtn(page).click() // -> back to markdown
  expect(await readSource(page)).toBe(md)
})

test('in markdown mode the Attach-file block does not overlap the editor', async ({
  page,
}) => {
  await login(page)
  await openFreshNoteEditor(page)
  await enterMarkdownMode(page)
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
  // ProseMirror command and there was no ProseMirror. They are source
  // transformations now, so the same buttons drive both surfaces.
  await login(page)
  await openFreshNoteEditor(page)
  await enterMarkdownMode(page)

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
  await enterMarkdownMode(page)
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
  await sourceContent(page).click()
  await page.keyboard.press('ControlOrMeta+ArrowUp')
  for (let i = 0; i < at; i += 1) await page.keyboard.press('ArrowRight')
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
  await enterMarkdownMode(page)

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
