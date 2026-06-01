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

export type UploadedAttachment = {
  id: string
  filename: string
  mimeType: string
  // Auth-protected markdown url (`/attachments/<id>/download`) — the same
  // route the Attachments panel and embedded images use. The Markdown
  // renderer / editor resolve it through authFetch; it is never public.
  url: string
}

export type UploadedImage = UploadedAttachment

export function isImageMime(mime: string | undefined | null): boolean {
  return !!mime && mime.startsWith('image/')
}

function attachmentsBase(parent: ImageUploadParent): string {
  return parent.kind === 'note'
    ? `/notes/${parent.id}/attachments`
    : `/tasks/${parent.id}/attachments`
}

/**
 * Upload `file` as an attachment of the parent note/task. Accepts ANY
 * mime — an upload is always just an attachment (the backend gates on
 * size only); whether it later renders inline or as a link is decided by
 * the returned `mimeType`, not at upload time. Returns the auth-protected
 * markdown url; nothing is exposed without the session token.
 */
export async function uploadAttachment(
  parent: ImageUploadParent,
  file: File,
): Promise<UploadedAttachment> {
  const body = new FormData()
  body.append('file', file)
  const res = await authFetch(attachmentsBase(parent), { method: 'POST', body })
  if (!res.ok) {
    const err = await res.json().catch(() => null)
    throw new Error(errMessage(err))
  }
  const data = (await res.json()) as {
    id: string
    filename: string
    mime_type: string
  }
  return {
    id: data.id,
    filename: data.filename,
    mimeType: data.mime_type,
    url: `/attachments/${data.id}/download`,
  }
}

/** Back-compat alias: the image drop/paste path still calls this. */
export const uploadImage = uploadAttachment
