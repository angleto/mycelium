import { useTranslation } from 'react-i18next'
import { WorkspaceManager } from '../components/WorkspaceManager'

export function WorkspaceRoute() {
  const { t } = useTranslation()
  return (
    <>
      <h1 className="page-title">{t('members.nav')}</h1>
      <WorkspaceManager />
    </>
  )
}
