import { authFetch, errMessage } from '../api/client'

export type ImageUploadParent =
  | { kind: 'note'; id: string }
  | { kind: 'task'; id: string }

// Image mimes the SPA is willing to upload from the editor. The backend
// /attachments routes accept any file; gating is purely client-side so
// drag/paste/picker only fire for things the markdown <img> can render.
export const ACCEPTED_IMAGE_MIME: readonly string[] = [
  'image/png',
  'image/jpeg',
  'image/gif',
  'image/webp',
  'image/svg+xml',
  'image/bmp',
]

const EXT_RE = /\.(png|jpe?g|gif|webp|svg|bmp)$/i

export function isAcceptedImage(file: File): boolean {
  if (file.type && file.type.startsWith('image/')) return true
  // Some OS/drag sources hand over an empty mime; fall back to ext.
  return EXT_RE.test(file.name)
}

export type UploadedImage = {
  id: string
  filename: string
  url: string
}

/**
 * Upload `file` as an attachment of the parent note/task and return a
 * markdown-friendly url (`/attachments/<id>/download`). The url is the
 * same auth-protected route the Attachments panel uses; the Markdown
 * renderer recognises it and resolves it through useAuthBlobUrl.
 */
export async function uploadImage(
  parent: ImageUploadParent,
  file: File,
): Promise<UploadedImage> {
  const base =
    parent.kind === 'note'
      ? `/notes/${parent.id}/attachments`
      : `/tasks/${parent.id}/attachments`
  const body = new FormData()
  body.append('file', file)
  const res = await authFetch(base, { method: 'POST', body })
  if (!res.ok) {
    const err = await res.json().catch(() => null)
    throw new Error(errMessage(err))
  }
  const data = (await res.json()) as { id: string; filename: string }
  return {
    id: data.id,
    filename: data.filename,
    url: `/attachments/${data.id}/download`,
  }
}
