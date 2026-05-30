import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { toAnchors, useAnnotations } from '../lib/useAnnotations'
import { AnnotationsPanel } from './AnnotationsPanel'
import { RichEditor } from './RichEditor'

// One note part's editor with its inline annotation layer. Owns the
// shared useAnnotations fetch for this part so the editor's inline UX
// (the floating 💬/✎ toolbar + the click-on-mark action popover) and the
// collapsible overview panel read the same rows and refresh together
// after a mutation. The editing value/onChange flow is forwarded
// untouched from NotePartsEditor, so the autosave/caret logic there is
// unaffected.
interface Props {
  partId: string
  value: string
  onChange: (v: string) => void
  placeholder?: string
  filename?: string
  /** Refresh the part body after a suggestion is accepted (its body
   * changed server-side, and any local draft must be dropped so the
   * editor adopts the spliced text). Wired to NotePartsEditor. */
  onDocMutated?: () => void | Promise<void>
}

export function PartAnnotated({
  partId,
  value,
  onChange,
  placeholder,
  filename,
  onDocMutated,
}: Props) {
  const { t } = useTranslation()
  const { rows, reload, error } = useAnnotations('note_part', partId)
  const [open, setOpen] = useState(false)
  const anchors = useMemo(() => toAnchors(rows), [rows])

  return (
    <>
      <RichEditor
        value={value}
        onChange={onChange}
        placeholder={placeholder}
        filename={filename}
        annotations={anchors}
        inlineAnnotations={{ docKind: 'note_part', docId: partId, rows, reload, onDocMutated }}
      />
      <div className="parts-editor__comments">
        <button
          type="button"
          className="btn--sm btn--ghost"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
        >
          {t('notes.parts.comments', { defaultValue: '💬 Comments' })} ({rows.length})
        </button>
        {open && (
          <AnnotationsPanel
            docKind="note_part"
            docId={partId}
            rows={rows}
            reload={reload}
            loadError={error}
            onDocMutated={onDocMutated}
          />
        )}
      </div>
    </>
  )
}
