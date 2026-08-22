import { useTranslation } from 'react-i18next'
import { EmbedderProviderSettings } from '../components/EmbedderProviderSettings'
import { LlmProviderSettings } from '../components/LlmProviderSettings'
import { MemoryChannelsAdmin } from '../components/MemoryChannelsAdmin'
import { SdiSettings } from '../components/SdiSettings'
import { useMe } from '../auth/useMe'
import { useAdminMode } from '../auth/useSession'

// Settings → Platform: one value for the whole deployment (model
// providers and their credentials, the memory-channel taxonomy, the SdI
// environment). Admin-only, and only while actually elevated — the same
// sudo rule the rest of the app follows.
//
// The tab is hidden when not entitled; this guard is what makes a
// hand-typed /settings/platform say so instead of rendering an empty
// page (the server re-checks every call regardless).
export function SettingsPlatformRoute() {
  const { t } = useTranslation()
  const { me } = useMe()
  const elevated = useAdminMode()

  if (!me?.is_admin || !elevated) {
    return (
      <section className="card">
        <h2>{t('setnav.platform')}</h2>
        <p className="hint">{t('setnav.platformHint')}</p>
      </section>
    )
  }

  return (
    <>
      <section className="models-group" aria-labelledby="models-group-title">
        <h2 id="models-group-title">{t('models.groupTitle')}</h2>
        <p className="hint">{t('models.groupHint')}</p>
        <LlmProviderSettings />
        <EmbedderProviderSettings />
      </section>
      <MemoryChannelsAdmin />
      <SdiSettings />
    </>
  )
}
