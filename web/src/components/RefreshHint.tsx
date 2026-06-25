import { useTranslation } from 'react-i18next'

/**
 * Non-destructive "changed elsewhere" affordance. Shown when a
 * version-diff probe (see useStaleWatch) detected that the open note or
 * task was written out-of-band — typically by an MCP tool, the CLI, or
 * another device. Reloading is always user-initiated: with unsaved local
 * edits we warn before discarding them (the 409-on-write policy already
 * refuses to silently auto-merge), so the user can choose to keep typing.
 */
export function RefreshHint({
  dirty,
  onReload,
  onDismiss,
}: {
  dirty: boolean
  onReload: () => void
  onDismiss: () => void
}) {
  const { t } = useTranslation()
  return (
    <div className="banner" role="status" aria-live="polite">
      <span>
        {dirty
          ? t('common.changedElsewhereDirty', {
              defaultValue:
                'Changed elsewhere. Reloading discards your unsaved edits.',
            })
          : t('common.changedElsewhere', {
              defaultValue: 'This was changed elsewhere.',
            })}
      </span>{' '}
      <button type="button" className="btn--sm" onClick={onReload}>
        {dirty
          ? t('common.reloadAnyway', { defaultValue: 'Reload anyway' })
          : t('common.reload', { defaultValue: 'Reload' })}
      </button>{' '}
      <button type="button" className="btn--sm btn--ghost" onClick={onDismiss}>
        {t('common.dismiss', { defaultValue: 'Dismiss' })}
      </button>
    </div>
  )
}
