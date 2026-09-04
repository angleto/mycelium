import { NavLink, Outlet } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { useMe } from '../auth/useMe'
import { useAdminMode } from '../auth/useSession'

// Settings, split by WHAT a setting belongs to rather than by who
// happened to add it:
//
//   Account    — you, on this login. Follows you into every workspace.
//   Browser    — this browser. Which one holds an extension credential is
//                not a property of you (it does not follow you to another
//                machine) nor of the workspace (which does not care where
//                you read it from), so it is its own scope rather than a
//                drawer inside one of the others.
//   Workspace  — this tenant. Different in the next workspace you open.
//   Platform   — the deployment. Admin-only, one value for everybody.
//
// The split is not cosmetic. The single flat page mixed all three, so
// "Attachment limit" (per workspace) sat next to "Time zone" (per user)
// with nothing to say that changing the first one changes it for your
// colleagues and only in the workspace you are currently in. The
// Workspace tab now leads with WHICH workspace it is configuring.
//
// The Platform tab is only offered while an admin is actually elevated;
// `useAdminMode` (reactive) rather than the imperative `isAdminMode()`
// the flat page used, so toggling the mode chip updates it immediately.
export function SettingsLayout() {
  const { t } = useTranslation()
  const { me } = useMe()
  const elevated = useAdminMode()
  const showPlatform = !!me?.is_admin && elevated

  const cls = ({ isActive }: { isActive: boolean }) =>
    isActive ? 'setnav__link setnav__link--active' : 'setnav__link'

  return (
    <>
      <h1 className="page-title">{t('nav.settings')}</h1>
      <nav className="setnav" aria-label={t('nav.settings')}>
        <NavLink end to="/settings" className={cls}>
          {t('setnav.account')}
        </NavLink>
        <NavLink to="/settings/extension" className={cls}>
          {t('setnav.extension')}
        </NavLink>
        <NavLink to="/settings/workspace" className={cls}>
          {t('setnav.workspace')}
        </NavLink>
        {showPlatform && (
          <NavLink to="/settings/platform" className={cls}>
            {t('setnav.platform')}
          </NavLink>
        )}
      </nav>
      <Outlet />
    </>
  )
}
