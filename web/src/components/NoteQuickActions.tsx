import { useTranslation } from 'react-i18next'
import { CopyIdButton } from './CopyIdButton'
import { FocusHereButton } from './FocusHereButton'
import { PeekButton } from './PeekButton'
import { PinButton } from './PinButton'
import type { components } from '../shared'

type TagBrief = components['schemas']['TagBrief']

// The controls that save a round trip through a note's page and change
// nothing in it: pin it above the recent ones, take its id, read it over the
// current view, narrow the app to its client and project. The notes list,
// the recent notes and the pinned ones show the same controls in the same
// order, so they live here once, as TaskQuickActions does for tasks.
//
// Bare, as a fragment: the wrapper and its geometry belong to the caller.
export function NoteQuickActions({
  id,
  title,
  tags,
}: {
  id: string
  title: string
  tags: TagBrief[] | undefined
}) {
  const { t } = useTranslation()
  return (
    <>
      <PinButton kind="note" id={id} />
      <CopyIdButton
        id={id}
        variant="icon"
        label={t('notes.copyId')}
        copiedLabel={t('notes.idCopied')}
      />
      <PeekButton target={{ kind: 'note', id, title }} />
      <FocusHereButton tags={tags} variant="icon" />
    </>
  )
}
