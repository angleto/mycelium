import type { Extension } from '@codemirror/state'
import { EditorView, ViewPlugin, type ViewUpdate } from '@codemirror/view'
import { classifyAttachmentRef, ensureAttachmentManifest, resolveAttachmentName } from '../attachmentManifest'
import { retainAuthBlob } from '../useAuthBlobUrl'
import type { ImageUploadParent } from '../imageUpload'

// Keeping an embedded image's bytes alive for the life of the editor.
//
// The blob cache behind `useAttachmentImage` is refcounted: the last
// consumer to let go revokes the object URL. That is exactly right for a
// page of React components, and exactly wrong for CodeMirror widgets, whose
// lifetime is the caret's business. Move the caret onto an image's line and
// the source is revealed, the widget is destroyed, its React root unmounts,
// the refcount hits zero and the bytes are thrown away; move it off again
// and the image is re-downloaded. Arrowing through a note with ten images
// re-downloads all ten, flashing each one.
//
// So the editor holds its own reference to every attachment its body
// mentions, for as long as it is open. Widget churn then moves the count
// between 1 and 2 instead of between 0 and 1, and nothing is ever revoked
// mid-session.
//
// This is a retain, not a second cache: it goes through the same refcount as
// every other consumer, so an image shown here and in the read-side view at
// the same time is still fetched once.

// Every `![...](dest)` in the document. A regex over the text rather than a
// syntax-tree walk on purpose: the tree is viewport-limited, and an image
// scrolled out of sight is exactly the one whose bytes must not be dropped.
const IMAGE_SRC_RE = /!\[(?:\\[\s\S]|[^\\\]\n])*\]\(\s*(<[^<>\n]*>|[^\s()]*)/g

function srcsIn(doc: string): string[] {
  const out: string[] = []
  for (const m of doc.matchAll(IMAGE_SRC_RE)) {
    const raw = m[1]
    const dest = raw.startsWith('<') && raw.endsWith('>') ? raw.slice(1, -1) : raw
    const src = dest.replace(/\\([\s\S])/g, '$1')
    if (src) out.push(src)
  }
  return out
}

/**
 * The bearer-auth paths an editor showing `doc` must hold on to: the blob
 * cache's keys for every image the body embeds.
 *
 * A bare filename needs the parent's attachment manifest, which may not be
 * loaded yet. Anything unresolved is simply not retained this round: the
 * widget's own fetch kicks the manifest load, and the next document change
 * (or editor mount) picks it up. Missing a retain costs a re-download;
 * guessing a path would poison the cache.
 */
export function attachmentAuthPaths(
  doc: string,
  parent: ImageUploadParent | undefined,
): string[] {
  const out = new Set<string>()
  let needsManifest = false
  for (const src of srcsIn(doc)) {
    const kind = classifyAttachmentRef(src)
    if (kind === 'auth') {
      out.add(src)
      continue
    }
    if (kind !== 'name' || !parent) continue
    const hit = resolveAttachmentName(parent, src)
    if (hit) out.add(hit)
    else needsManifest = true
  }
  if (needsManifest && parent) void ensureAttachmentManifest(parent)
  return Array.from(out)
}

/**
 * Retain every attachment the body embeds, for as long as this editor lives.
 *
 * `getParent` is read live (through the host's ref) rather than captured:
 * the parent id is not known when a brand-new note's editor is built, and an
 * editor built once must not be stuck with the value from that moment.
 */
export function attachmentRetain(
  getParent: () => ImageUploadParent | undefined,
): Extension {
  return ViewPlugin.fromClass(
    class {
      /** path -> release. The set of paths currently retained. */
      private held = new Map<string, () => void>()

      constructor(view: EditorView) {
        this.sync(view)
      }

      update(u: ViewUpdate) {
        // Only the document can change which attachments are referenced.
        // Scrolling and moving the caret must not, or this would defeat its
        // own purpose.
        if (u.docChanged) this.sync(u.view)
      }

      destroy() {
        for (const release of this.held.values()) release()
        this.held.clear()
      }

      private sync(view: EditorView) {
        const wanted = new Set(attachmentAuthPaths(view.state.sliceDoc(), getParent()))
        for (const [path, release] of this.held) {
          if (!wanted.has(path)) {
            release()
            this.held.delete(path)
          }
        }
        for (const path of wanted) {
          if (!this.held.has(path)) this.held.set(path, retainAuthBlob(path))
        }
      }
    },
  )
}
