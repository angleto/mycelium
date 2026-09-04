// Getting what is in front of you into Mycelium.
//
// Reading the page is CAPABILITY-BASED, never a list of URLs Chrome is
// known to refuse: that list rots between releases, and the failure of a
// stale one is silent. Try the injection, catch, degrade.
//
// Capture never blocks. The title and the URL come from the tab record
// and need no injection at all, so a page that refuses scripting costs
// the SELECTION and nothing else -- which is a notice, not an error.

import { call } from './api'
import { entityRoute } from './config'
import { code } from './find'
import type { CaptureDraft, CaptureResult, PageContext, Result } from '../shared/protocol'
import type { StoredConnection } from './storage'

/** The schemes a captured link may carry, enumerated rather than
 *  filtered: the URL reaches a markdown renderer in the app, the CLI and
 *  the editor plugin, and there the scheme is syntax. Choosing from a set
 *  cannot be defeated by a scheme nobody thought of. */
const LINKABLE = new Set(['http:', 'https:'])

/** Markdown link text is a small language; a title containing brackets or
 *  a newline would otherwise close the link early and leave the rest as
 *  prose. */
function escapeLinkText(raw: string): string {
  return raw.replace(/[\\[\]]/g, '\\$&').replace(/\s+/g, ' ').trim()
}

export function sourceLine(url: string | null, title: string | null): string {
  if (!url) return ''
  let parsed: URL
  try {
    parsed = new URL(url)
  } catch {
    return ''
  }
  if (!LINKABLE.has(parsed.protocol)) return ''
  const text = escapeLinkText(title || parsed.host)
  // Parentheses in the target would close the link early, and a URL may
  // legitimately contain them (a Wikipedia disambiguation, a generated
  // report path). Mapped explicitly rather than through
  // encodeURIComponent, which leaves both alone: they are unreserved
  // characters, so that call is a no-op here and reads like a fix.
  const target = parsed.toString().replaceAll('(', '%28').replaceAll(')', '%29')
  return `[${text}](${target})`
}

export async function pageContext(): Promise<PageContext> {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true })
  const url = tab?.url ?? null
  const title = tab?.title ?? null
  if (!tab?.id) return { url, title, selection: null, selectionBlocked: true }
  try {
    const [result] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      // This body is serialised and runs in the PAGE, not here. The
      // worker is compiled without a DOM on purpose, so the selection
      // API is reached through globalThis rather than by giving this
      // file a document it does not have.
      func: () => {
        const sel = (globalThis as { getSelection?: () => { toString(): string } | null })
          .getSelection?.()
        return sel ? sel.toString() : ''
      },
    })
    const selection = (result?.result as string | undefined) ?? ''
    return { url, title, selection: selection || null, selectionBlocked: false }
  } catch {
    // A Chrome page, the extension gallery, a PDF, a file:// URL without
    // permission. The tab record still has a title and a URL worth
    // keeping.
    return { url, title, selection: null, selectionBlocked: true }
  }
}

/** A photograph of the visible viewport of the tab the extension was
 *  invoked from. `activeTab` is granted for THAT tab and does not follow
 *  a tab switch, so a side panel asked to photograph a tab it was not
 *  invoked from is refused -- and says why, rather than failing blankly. */
export async function screenshot(): Promise<Result<{ name: string; mime: string; dataUrl: string }>> {
  try {
    const dataUrl = await chrome.tabs.captureVisibleTab({ format: 'png' })
    return { ok: true, data: { name: 'page.png', mime: 'image/png', dataUrl } }
  } catch (cause) {
    return {
      ok: false,
      error: {
        code: 'forbidden',
        message: cause instanceof Error ? cause.message : String(cause),
        retryable: false,
      },
    }
  }
}

function blobFromDataUrl(dataUrl: string, mime: string): Blob {
  const base64 = dataUrl.slice(dataUrl.indexOf(',') + 1)
  const binary = atob(base64)
  const bytes = new Uint8Array(binary.length)
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i)
  return new Blob([bytes], { type: mime })
}

interface CreatedTask {
  id: string
}

/** Two phases, and the panel is told about both.
 *
 *  The entity is created, then each attachment is uploaded on its own
 *  request. If an upload fails the entity still exists, and saying
 *  "created, one file did not attach" is the truth -- a blanket failure
 *  would send someone hunting for a task that is already there and let
 *  them create a second one. */
export async function create(
  conn: StoredConnection,
  draft: CaptureDraft,
): Promise<Result<CaptureResult>> {
  const created =
    draft.kind === 'task'
      ? await call<CreatedTask>(conn, '/tasks', {
          method: 'POST',
          idempotencyKey: draft.idempotencyKey,
          body: {
            title: draft.title,
            description: draft.body || undefined,
            project_tag_id: draft.projectTagId ?? undefined,
            client_tag_id: draft.clientTagId ?? undefined,
          },
        })
      : await call<CreatedTask>(conn, '/notes', {
          method: 'POST',
          idempotencyKey: draft.idempotencyKey,
          body: {
            title: draft.title || undefined,
            text: draft.body,
            kind: 'text',
            project_tag_id: draft.projectTagId ?? undefined,
            client_tag_id: draft.clientTagId ?? undefined,
          },
        })
  if (!created.ok) return created

  const id = created.data.id
  const failed: string[] = []
  for (const file of draft.attachments) {
    const form = new FormData()
    form.append('file', blobFromDataUrl(file.dataUrl, file.mime), file.name)
    const up = await call<unknown>(
      conn,
      `/${draft.kind === 'task' ? 'tasks' : 'notes'}/${encodeURIComponent(id)}/attachments`,
      { method: 'POST', form },
    )
    if (!up.ok) failed.push(file.name)
  }

  return {
    ok: true,
    data: {
      kind: draft.kind,
      id,
      code: code(id),
      route: entityRoute(draft.kind, code(id)),
      attachmentsFailed: failed,
    },
  }
}
