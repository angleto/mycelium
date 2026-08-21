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

/** Assert the editor is in markdown-source mode. */
export async function expectSourceMode(page: Page, nth = 0) {
  await expect(sourceContent(page, nth)).toBeVisible()
}

/** Is the source surface currently mounted? (The mode toggle's state.) */
export async function inSourceMode(page: Page, nth = 0): Promise<boolean> {
  return sourceContent(page, nth)
    .isVisible()
    .catch(() => false)
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
 * The document as text, rebuilt from the rendered lines.
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
  const lines = await page.locator('.rte__src').nth(nth).locator('.cm-line').allTextContents()
  return lines.join('\n')
}
