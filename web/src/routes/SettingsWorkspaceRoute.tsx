import { useTranslation } from 'react-i18next'
import { AiAssistantsSettings } from '../components/AiAssistantsSettings'
import { AttachmentSettings } from '../components/AttachmentSettings'
import { EstimatePresets } from '../components/EstimatePresets'
import { GmailConnect } from '../components/GmailConnect'
import { IssuerProfiles } from '../components/IssuerProfiles'
import { RetrievalSettings } from '../components/RetrievalSettings'
import { WorkspaceIdentityCard } from '../components/WorkspaceIdentityCard'
import { WorkspaceListCard } from '../components/WorkspaceListCard'
import { WorkspaceMembersCard } from '../components/WorkspaceMembersCard'
import { useMyWorkspace } from '../auth/useMyWorkspace'
import { useWorkspaceRole } from '../auth/useSession'
import { canWriteWorkspace } from '../lib/workspaceChoice'

// Settings → Workspace: this tenant, and only this tenant.
//
// The page is ordered identity → people → lifecycle → configuration, so
// it always answers "which workspace am I changing?" before it offers
// anything that changes it.
//
// Everything under the configuration group is stored in the workspace's
// settings bag (or in org-scoped rows) and is therefore SHARED with
// every member: the mail account the workspace syncs, its AI assistants
// and their scopes, its invoicing issuers, its estimate presets, its
// retrieval thresholds, its attachment cap. Retrieval and attachments
// used to be reachable only by a platform admin in sudo mode even
// though the server has always gated them on the workspace OWNER —
// the gate here now matches the one the server enforces.
export function SettingsWorkspaceRoute() {
  const { t } = useTranslation()
  const { ws } = useMyWorkspace()
  const requested = useWorkspaceRole()
  const canWrite = canWriteWorkspace(ws?.my_role ?? 'member', requested)

  return (
    <>
      <WorkspaceIdentityCard />
      <WorkspaceMembersCard />
      <WorkspaceListCard />

      <section className="models-group" aria-labelledby="wsconf-group-title">
        <h2 id="wsconf-group-title">{t('wsmgr.configTitle')}</h2>
        <p className="hint">{t('wsmgr.configHint')}</p>
        {!canWrite && <p className="hint">{t('members.manageHint')}</p>}
        <GmailConnect />
        <AiAssistantsSettings />
        <IssuerProfiles />
        <EstimatePresets />
        <RetrievalSettings />
        <AttachmentSettings />
      </section>
    </>
  )
}
