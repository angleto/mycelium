import { useAttachmentImage } from '../useAuthBlobUrl'
import type { ImageUploadParent } from '../imageUpload'

// The inside of an image widget in the markdown source editor.
//
// Its own file, and a real component rather than DOM built by hand, because
// resolving an attachment reference is not a one-liner: an
// `/attachments/<id>/download` src is bearer-authenticated (a plain <img>
// would 401), a bare filename has to be resolved against this note's or
// task's own attachments through a manifest that may still be loading, and a
// fetch that fails transiently is retried under the hood. All of that lives
// in `useAttachmentImage`, and having one implementation of it is worth a
// React root per image.
//
// The class names are the ones the read-side renderer already uses, so an
// embed looks the same in the editor and in the reader.
export function AttachmentImage({
  src,
  alt,
  title,
  parent,
}: {
  src: string
  alt: string
  title?: string
  parent?: ImageUploadParent
}) {
  const { url, loading } = useAttachmentImage(src, parent)
  if (loading) return <span className="md-img md-img--loading" />
  // Broken is a real state, not an eternal spinner: an unknown filename, a
  // deleted attachment, a fetch that gave up. Show what the author wrote.
  if (!url) return <span className="md-img md-img--broken">{alt || src || '?'}</span>
  return (
    <span className="md-img-wrap">
      <img src={url} alt={alt} title={title} className="md-img" />
    </span>
  )
}
