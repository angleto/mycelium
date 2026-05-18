import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../api/client'
import { setActiveWorkspace } from '../auth/session'
import { useSession } from '../auth/useSession'
import type { components } from '../api/schema'

type Workspace = components['schemas']['WorkspaceSummaryOut']

// Sidebar: just the quick switch (ADR-0024 — switching is only an
// active-id change, no re-auth). Creating / archiving / deleting a
// workspace lives in Settings → workspace block, not in the menu.
export function WorkspaceSwitcher() {
  const { t } = useTranslation()
  const session = useSession()
  const [list, setList] = useState<Workspace[]>([])

  useEffect(() => {
    let active = true
    void (async () => {
      const { data } = await api.GET('/workspaces')
      if (active && data) setList(data)
    })()
    return () => {
      active = false
    }
  }, [session?.workspaceId])

  return (
    <div className="switcher">
      <label>
        {t('switcher.label')}{' '}
        <select
          value={session?.workspaceId ?? ''}
          onChange={(e) => setActiveWorkspace(e.target.value)}
        >
          {list
            .filter(
              (w) => w.status !== 'archived' || w.id === session?.workspaceId,
            )
            .map((w) => (
              <option key={w.id} value={w.id}>
                {w.name}
                {w.status === 'archived' ? ` (${t('wsmgr.archived')})` : ''}
              </option>
            ))}
        </select>
      </label>
    </div>
  )
}
