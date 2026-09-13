// Putting a string on the clipboard, in the one place that knows how.
//
// Lifted out of Attachments.tsx, which had the only version that survives
// a real browser. Four other call sites (the two detail headers, the
// garden modal, the parts editor) each carried a bare
// `navigator.clipboard.writeText` inside a try/catch that swallowed the
// failure, so on a plain-http deployment, or with the permission denied,
// they copied nothing and said "Copied" anyway. Sharing this is what makes
// a copy button honest wherever it appears, and the reason it matters now
// is that there are more of them, not fewer: the id moved onto the cards.

/**
 * Put `text` on the clipboard, degrading through every path a browser may
 * still leave open. The async Clipboard API is unavailable outside a
 * secure context (a plain-http deployment has no `navigator.clipboard` at
 * all) and rejects when the permission is denied or the document is not
 * focused, so:
 *   1. navigator.clipboard.writeText — the modern, permissioned path;
 *   2. a throwaway textarea + execCommand('copy') — deprecated, but the
 *      only thing that works on http. Still inside the click gesture, so
 *      the transient user activation it requires is alive;
 *   3. window.prompt with the text preselected — no automatic copy, yet
 *      the string is in front of the user, who can select and copy it.
 *
 * Returns whether the text reached the clipboard WITHOUT manual work, so
 * the caller can be honest in the UI instead of flashing a lying "Copied".
 * The one thing that never happens is a silent no-op.
 *
 * `manualPrompt` is the message for path 3, resolved from the catalogue by
 * the caller: this module has no business holding user-facing text.
 */
export async function copyToClipboard(text: string, manualPrompt: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch {
    /* insecure context / denied / unfocused — try the fallbacks */
  }
  // Built outside the try so the finally below can always take it back
  // out: if execCommand throws, a textarea left in the body would keep
  // the (focused, off-screen) selection and swallow the keyboard.
  const ta = document.createElement('textarea')
  try {
    ta.value = text
    // Off-screen but still rendered and focusable: `hidden` or
    // display:none makes the selection — and therefore the copy — a
    // no-op. readonly keeps the mobile keyboard from popping up.
    ta.setAttribute('readonly', '')
    ta.style.position = 'fixed'
    ta.style.top = '-1000px'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.select()
    if (document.execCommand('copy')) return true
  } catch {
    /* execCommand unsupported or blocked — fall through to the prompt */
  } finally {
    ta.remove()
  }
  try {
    window.prompt(manualPrompt, text)
  } catch {
    /* modals blocked (sandboxed frame): the failed badge is all we have */
  }
  return false
}
