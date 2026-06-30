import { useEffect, useState } from 'react'
import { api, workspaceHeader } from '../api/client'
import { useSession } from '../auth/useSession'
import type { components } from '../api/schema'

type State = components['schemas']['StateOut']

// Load the states of the workspace's default workflow. Shared across
// /tasks, /graph, /time, and any other route that needs the
// is_terminal / is_hidden flags or the canonical state list. Picks
// ``is_default = true`` first, falls back to the first row. Returns
// an empty array until the fetch settles.
//
// The default workflow is the project-agnostic baseline; per-project
// overrides exist (workflow_project_overrides) but every route that
// uses this hook today operates at the workspace level, not per
// project, so the default is the right pick. A future "per-project
// states" hook would supersede this for that surface.
export function useWorkflowStates(): State[] {
  const session = useSession()
  const activeId = session?.workspaceId
  const [states, setStates] = useState<State[]>([])

  useEffect(() => {
    if (!activeId) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setStates([])
      return
    }
    let active = true
    void (async () => {
      const h = workspaceHeader()
      const wfs = await api.GET('/workflows', { params: { header: h } })
      if (!active || !wfs.data || wfs.data.length === 0) return
      const def = wfs.data.find((w) => w.is_default) ?? wfs.data[0]
      const st = await api.GET('/workflows/{workflow_id}/states', {
        params: { header: h, path: { workflow_id: def.id } },
      })
      if (!active) return
      if (st.data) setStates(st.data)
    })()
    return () => {
      active = false
    }
  }, [activeId])

  return states
}
