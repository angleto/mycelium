import { authFetch } from '../api/client'
import type { ImageUploadParent } from './imageUpload'

// Resolve attachment references written by *filename* in a note/task
// body, e.g. `![FIGURA](Fig02_donne_meno_reddito.png)`.
//
// The canonical reference the picker/upload inserts is the opaque
// bearer-auth URL `/attachments/<id>/download`. But humans authoring
// markdown naturally write the bare filename of a file they uploaded to
// the same note/task (the way co-located image files are referenced in
// any markdown document). Without a filename->id map such a reference is
// a dead relative URL: the <img> 404s (a perpetual broken/loading box)
// and the image never appears.
//
// This module fetches the parent's attachment list once, keyed by
// parent, and maps each filename (basename, case-insensitive) to its id
// so both the editor live-preview and the read-side renderer can turn a
// filename into the same `/attachments/<id>/download` URL every other
// path already understands. Nothing here exposes a public URL — the
// resolved URL is still the bearer-auth route resolved through authFetch.

type Manifest = Map<string, string> // basename(lowercased) -> attachment id

const cache = new Map<string, Manifest>()
const inflight = new Map<string, Promise<Manifest>>()

function keyOf(parent: ImageUploadParent): string {
  return `${parent.kind}:${parent.id}`
}

function baseOf(parent: ImageUploadParent): string {
  return parent.kind === 'note'
    ? `/notes/${parent.id}/attachments`
    : `/tasks/${parent.id}/attachments`
}

// Reduce a reference (or a stored filename) to a comparable basename:
// drop any query/hash, take the last path segment, percent-decode, trim
// and lowercase. So `./images/Fig02.png?v=1` and `Fig02.PNG` both match
// a stored `Fig02.png`.
export function attachmentBasename(ref: string): string {
  const noFragment = ref.split(/[?#]/, 1)[0]
  const seg = noFragment.split('/').pop() ?? noFragment
  let decoded = seg
  try {
    decoded = decodeURIComponent(seg)
  } catch {
    // Malformed escape: fall back to the raw segment.
  }
  return decoded.trim().toLowerCase()
}

// How a markdown image src / link href relates to an attachment:
//  - 'auth'     already the canonical /attachments/<id>/download route
//  - 'absolute' a URL (scheme:…), an absolute path, or a #fragment — used
//               verbatim, never an attachment
//  - 'name'     a bare filename to resolve against the parent's files
//  - 'empty'    nothing
export type AttachmentRefKind = 'empty' | 'auth' | 'absolute' | 'name'

export function classifyAttachmentRef(
  ref: string | undefined | null,
): AttachmentRefKind {
  if (!ref) return 'empty'
  if (ref.startsWith('/attachments/')) return 'auth'
  // A URI scheme (http:, https:, data:, blob:, mailto:…), an absolute
  // path, or a fragment is taken as-is.
  if (/^[a-zA-Z][a-zA-Z\d+.-]*:/.test(ref)) return 'absolute'
  if (ref.startsWith('/') || ref.startsWith('#')) return 'absolute'
  return 'name'
}

// Whether the parent's manifest has finished loading (so a null from
// resolveAttachmentName means "no such filename" rather than "not yet
// fetched").
export function isAttachmentManifestLoaded(parent: ImageUploadParent): boolean {
  return cache.has(keyOf(parent))
}

// Synchronous lookup against the already-loaded manifest. Returns the
// canonical auth URL for a filename reference, or null when the manifest
// is not loaded yet or no attachment matches that name.
export function resolveAttachmentName(
  parent: ImageUploadParent,
  ref: string,
): string | null {
  const manifest = cache.get(keyOf(parent))
  if (!manifest) return null
  const id = manifest.get(attachmentBasename(ref))
  return id ? `/attachments/${id}/download` : null
}

// Fetch + cache the parent's attachment manifest (one round-trip per
// parent, shared across all references on the page). A failed fetch
// caches an empty manifest so we don't hammer the endpoint; call
// invalidateAttachmentManifest after an upload to pick up new files.
export function ensureAttachmentManifest(
  parent: ImageUploadParent,
): Promise<Manifest> {
  const key = keyOf(parent)
  const have = cache.get(key)
  if (have) return Promise.resolve(have)
  const flying = inflight.get(key)
  if (flying) return flying

  const promise = (async (): Promise<Manifest> => {
    const manifest: Manifest = new Map()
    try {
      const res = await authFetch(baseOf(parent))
      if (res.ok) {
        const rows = (await res.json()) as Array<{
          id: string
          filename: string
        }>
        for (const r of rows) {
          if (r.filename) manifest.set(attachmentBasename(r.filename), r.id)
        }
      }
    } catch {
      // Network error: cache the empty manifest; an upload (which calls
      // invalidate) or a later reload is the recovery path.
    }
    cache.set(key, manifest)
    inflight.delete(key)
    return manifest
  })()
  inflight.set(key, promise)
  return promise
}

// Drop the cached manifest so the next resolution refetches. Call after
// uploading a new attachment to the parent so a freshly added file
// resolves by name without a page reload.
export function invalidateAttachmentManifest(parent: ImageUploadParent): void {
  const key = keyOf(parent)
  cache.delete(key)
  inflight.delete(key)
}
