import { useTranslation } from 'react-i18next'
import { useBuildWatch } from '../lib/useBuildWatch'

/**
 * "A newer version of the app is deployed" affordance.
 *
 * Only ever rendered in the cases where useBuildWatch deliberately did
 * NOT reload on its own: unsaved edits are open, or a reload for this
 * build was already attempted (rolling update). The silent case — clean
 * app, new build — never reaches here, it just reloads.
 */
export function UpdateBanner() {
  const { t } = useTranslation()
  const { newBuildId, reloadNow, dismiss } = useBuildWatch()
  if (!newBuildId) return null
  return (
    <div className="banner banner--update" role="status" aria-live="polite">
      <span>{t('common.newVersion')}</span>{' '}
      <button type="button" className="btn--sm" onClick={reloadNow}>
        {t('common.reloadAnyway')}
      </button>{' '}
      <button type="button" className="btn--sm btn--ghost" onClick={dismiss}>
        {t('common.dismiss')}
      </button>
    </div>
  )
}
