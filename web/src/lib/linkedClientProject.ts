import { useState } from 'react'
import type { components } from '../api/schema'

type Project = components['schemas']['ProjectOut']

/**
 * Shared logic for the Client/Project picker pair (Tasks quick-add,
 * Time filters, the TagPicker's structural row).
 *
 * It is the client-side half of the ADR-0050 invariant: an entity's
 * client is the one its project belongs to. The server enforces it
 * (services/tag_assignment); this only keeps the two selects honest
 * while the user is still choosing.
 *
 * - Filters the project list by the selected client (when a client is
 *   picked, only projects whose `client_tag_id` matches show up).
 * - When the user picks a project, snaps the client to that project's
 *   `client_tag_id` (so a project tied to client X always lives under X).
 * - When the user picks a client that does not match the currently
 *   selected project, drops the stale project selection.
 *
 * Both pickers are stateful here; the caller wires `value` + `onChange`
 * to its own UI (select / TagPicker / etc.).
 */
export function useLinkedClientProject(profiles: Project[]): {
  clientId: string
  projectId: string
  onPickClient: (id: string) => void
  onPickProject: (id: string) => void
  setClientId: (id: string) => void
  setProjectId: (id: string) => void
  filterProjectsByClient: <T extends { id: string }>(projects: T[]) => T[]
} {
  const [clientId, setClientId] = useState('')
  const [projectId, setProjectId] = useState('')

  function profileFor(projectTagId: string): Project | undefined {
    return profiles.find((x) => x.id === projectTagId)
  }

  function onPickProject(id: string): void {
    setProjectId(id)
    if (!id) return
    const prof = profileFor(id)
    if (prof?.client_tag_id && prof.client_tag_id !== clientId) {
      setClientId(prof.client_tag_id)
    }
  }

  function onPickClient(id: string): void {
    setClientId(id)
    if (!projectId) return
    const prof = profileFor(projectId)
    if (id && prof?.client_tag_id && prof.client_tag_id !== id) {
      setProjectId('')
    }
  }

  function filterProjectsByClient<T extends { id: string }>(projects: T[]): T[] {
    if (!clientId) return projects
    return projects.filter((p) => profileFor(p.id)?.client_tag_id === clientId)
  }

  return {
    clientId,
    projectId,
    onPickClient,
    onPickProject,
    setClientId,
    setProjectId,
    filterProjectsByClient,
  }
}
