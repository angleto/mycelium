import { expect, type Page } from '@playwright/test'

// Driving the markdown SOURCE surface from a spec.
//
// It used to be a `<textarea>`, so a spec could `.fill()` it and read
// `.inputValue()`. It is CodeMirror now, which means neither works: there is
// no `value`, and typing multi-line markdown through the keyboard goes
// through the Enter binding (`insertNewlineAndIndent`), which copies the
// previous line's indentation and would corrupt exactly the fixtures these
// specs care about -- an indented code block, a nested list.
//
// So: write through a real paste event (CodeMirror inserts the pasted text
// verbatim, no indent logic), and read through the rendered lines.

/** The contenteditable CodeMirror writes into, for the Nth editor on the
 *  page (the first by default: a note has one editor per part). */
export function sourceContent(page: Page, nth = 0) {
  return page.locator('.rte__src .cm-content').nth(nth)
}

/** The view switch, by its accessible name. */
export function modeToggle(page: Page, nth = 0) {
  return page.getByRole('button', { name: 'Plain markdown' }).nth(nth)
}

/**
 * Which of the two views the editor is showing.
 *
 * From the EDITOR's own attribute, not from the button's `aria-pressed` and
 * not from whether `.cm-content` is visible. There is one surface, mounted in
 * both views, so visibility cannot tell them apart; and the button reflects
 * React state, which changes one commit before the editor is reconfigured, so
 * reading it would be racing the thing the caller is waiting for.
 */
export async function editorMode(page: Page, nth = 0): Promise<'source' | 'visual'> {
  const attr = await page.locator('.rte__src').nth(nth).getAttribute('data-md-mode')
  return attr === 'source' ? 'source' : 'visual'
}

/**
 * Put the editor in `mode`, whatever it was in.
 *
 * The preference is app-wide and persisted, so a spec that assumes a view has
 * to assert it rather than inherit whatever the previous spec left in
 * localStorage (one worker, one browser context, one origin).
 */
export async function setEditorMode(
  page: Page,
  mode: 'source' | 'visual',
  nth = 0,
): Promise<void> {
  if ((await editorMode(page, nth)) === mode) return
  await modeToggle(page, nth).click()
  await expect(page.locator('.rte__src').nth(nth)).toHaveAttribute('data-md-mode', mode)
}

/**
 * Replace the whole document with ``md``.
 *
 * Select-all, then a synthetic paste carrying the text: CodeMirror's paste
 * handler replaces the selection with the clipboard string exactly as given.
 * `pressSequentially` would be re-indented by the Enter binding, and
 * `fill()` on a contenteditable sets text without going through CodeMirror's
 * transaction system at all, so the editor's document would not change.
 */
export async function setSource(page: Page, md: string, nth = 0): Promise<void> {
  const content = sourceContent(page, nth)
  await content.click()
  await page.keyboard.press('ControlOrMeta+a')
  await content.evaluate((el, text) => {
    const dt = new DataTransfer()
    dt.setData('text/plain', text)
    el.dispatchEvent(
      new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true }),
    )
  }, md)
}

/**
 * Put the caret at an absolute offset in the SOURCE.
 *
 * NOT by counting ArrowRight from the document start. The live preview hides
 * markup and renders whole blocks as widgets, so a keypress crosses RENDERED
 * positions, not source characters: walking `n` times right lands further down
 * the document than offset `n` says, and the test that did it typed its
 * character three blocks away from where it meant to.
 *
 * Select-all first -- the editor's own reveal rule, the same one `readSource`
 * leans on: a selection covering the whole document touches every construct
 * in it, so every one of them shows its source and the rendered lines ARE the
 * source lines. A (line, column) resolved from the source text then addresses
 * the DOM faithfully. True in both modes: in markdown mode nothing was hidden
 * to begin with.
 *
 * Then set the browser selection there, which is where CodeMirror reads its
 * own from; collapsing the selection re-hides the other constructs' markup,
 * which changes what is DRAWN and never the document, so the offset stays
 * put.
 *
 * ``md`` is the source the editor currently holds: the caller has it (it
 * pasted it), and resolving the offset against it keeps this helper out of
 * the business of parsing what is on screen.
 */
export async function placeCaret(
  page: Page,
  md: string,
  offset: number,
  nth = 0,
): Promise<void> {
  const before = md.slice(0, offset)
  const line = before.split('\n').length - 1
  const column = offset - (before.lastIndexOf('\n') + 1)
  const content = sourceContent(page, nth)
  await content.click()
  await page.keyboard.press('ControlOrMeta+a')
  await page
    .locator('.rte__src')
    .nth(nth)
    .locator('.cm-line')
    .nth(line)
    .evaluate((el, col) => {
      // The line's text nodes in order; the column falls in one of them.
      const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT)
      let seen = 0
      let node = walker.nextNode() as Text | null
      let target: Text | null = node
      let inNode = col
      while (node) {
        if (seen + node.length >= col) {
          target = node
          inNode = col - seen
          break
        }
        seen += node.length
        target = node
        inNode = node.length
        node = walker.nextNode() as Text | null
      }
      const range = document.createRange()
      if (target) range.setStart(target, Math.min(inNode, target.length))
      else range.setStart(el, 0)
      range.collapse(true)
      const sel = window.getSelection()
      sel?.removeAllRanges()
      sel?.addRange(range)
    }, column)
}

/**
 * The document as text, rebuilt from the rendered lines.
 *
 * SELECT-ALL FIRST, and that is not a trick: in the rendered view the
 * live-preview layer hides the markup of every construct the selection does
 * not touch, so reading the rendered lines with the caret parked somewhere
 * gives the RENDERING (`Titolo`), not the source (`## Titolo`). A selection
 * covering the whole document touches every construct in it, so the whole
 * document shows its source, by the editor's own documented rule, and the
 * rendered lines then are the source. It reads the contract instead of
 * reaching around it. In markdown mode there is nothing to reveal and the
 * same code path is simply already true.
 *
 * One `.cm-line` per document line, in order, `textContent` faithful (the
 * content is `white-space: pre-wrap` and neither `highlightSpecialChars` nor
 * `highlightWhitespace` is configured, so nothing substitutes characters).
 * A document ending in a newline renders a final empty line, so joining with
 * `\n` reproduces the trailing newline.
 *
 * LIMIT, deliberately not worked around: CodeMirror renders the VIEWPORT, so
 * this is only the whole document for a body that fits on screen. Every
 * fixture here is a handful of lines. A byte-exactness assertion over a long
 * body belongs on the wire (the PATCH payload), not in the DOM.
 */
export async function readSource(page: Page, nth = 0): Promise<string> {
  const content = sourceContent(page, nth)
  await content.click()
  await page.keyboard.press('ControlOrMeta+a')
  const lines = await page.locator('.rte__src').nth(nth).locator('.cm-line').allTextContents()
  return lines.join('\n')
}
