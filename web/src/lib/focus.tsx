import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from 'react'

import { lastWorkspaceId } from '../auth/session'
import { useSession } from '../auth/useSession'

// Focus mode: narrow the project-scoped views (Notes, Tasks) to one
// CLIENT (all its projects) and, optionally, to a single project
// within it. Distraction-free, low-click.
//
// The sidebar owns the client→projects data, so it pushes the
// selected client's project-tag ids via setClientProjectIds; consumers
// only read `focusIds` (the effective project-tag-id allow-list) and
// `active`.
//
// TENANCY. A focus is a property of the ACTIVE WORKSPACE, not of the
// browser. A client/project tag id only means anything inside the
// workspace it was picked in: carried into another one it names nothing,
// so every scoped view filters down to empty while the picker still
// shows the previous tenant's client name (task 805a569c). Two things
// make that unrepresentable rather than merely unlikely:
//
//   - the record is persisted under a per-workspace key, so a reload
//     rehydrates the focus of the workspace you are actually in;
//   - the state is REMOUNTED when the workspace id changes, because
//     switching workspace is an in-app context switch and not a reload
//     (auth/session.setActiveWorkspace), so nothing here would otherwise
//     be re-read.
//
// Signing out lands on the anonymous key, which persists nothing: the
// next account on this browser inherits no selection.
const KEY = 'mycelium-focus'

// The pre-805a569c layout: three flat keys holding ONE selection shared
// by every workspace. Folded into the workspace they were picked in and
// removed, so another tenant's client name does not sit in localStorage
// for good.
const LEGACY_CLIENT = 'mycelium-focus-client'
const LEGACY_PROJECT = 'mycelium-focus-project'
const LEGACY_NAME = 'mycelium-focus-client-name'

// The client NAME travels with the id in one record on purpose. It used
// to live in its own key written by the sidebar, which let a name and an
// id from two different workspaces coexist and be rendered together.
type Rec = { clientId: string; clientName: string; projectId: string }

const EMPTY: Rec = { clientId: '', clientName: '', projectId: '' }

function recordKey(wsId: string): string {
  return `${KEY}:${wsId}`
}

function load(wsId: string): Rec {
  if (!wsId) return EMPTY
  try {
    const raw = localStorage.getItem(recordKey(wsId))
    if (!raw) return EMPTY
    const v = JSON.parse(raw) as Partial<Rec>
    return {
      clientId: typeof v.clientId === 'string' ? v.clientId : '',
      clientName: typeof v.clientName === 'string' ? v.clientName : '',
      projectId: typeof v.projectId === 'string' ? v.projectId : '',
    }
  } catch {
    return EMPTY
  }
}

function save(wsId: string, rec: Rec): void {
  if (!wsId) return
  if (!rec.clientId && !rec.projectId) localStorage.removeItem(recordKey(wsId))
  else localStorage.setItem(recordKey(wsId), JSON.stringify(rec))
}

function migrateLegacy(): void {
  try {
    const clientId = localStorage.getItem(LEGACY_CLIENT)
    const projectId = localStorage.getItem(LEGACY_PROJECT)
    const clientName = localStorage.getItem(LEGACY_NAME)
    if (clientId === null && projectId === null && clientName === null) return
    // The last active workspace is the one the selection was made in:
    // it is what the old flat keys were implicitly scoped to.
    const wsId = lastWorkspaceId() ?? ''
    if (wsId && (clientId || projectId)) {
      save(wsId, {
        clientId: clientId ?? '',
        clientName: clientName ?? '',
        projectId: projectId ?? '',
      })
    }
    localStorage.removeItem(LEGACY_CLIENT)
    localStorage.removeItem(LEGACY_PROJECT)
    localStorage.removeItem(LEGACY_NAME)
  } catch {
    // A browser that refuses localStorage simply starts unfocused.
  }
}

migrateLegacy()

type FocusCtx = {
  clientId: string
  projectId: string
  // Human-readable names of the active focus, for surfacing what the
  // current scope IS (e.g. the advisory "Scoped to …" chip). The client
  // name is stored with its id; the project name is derived by the
  // sidebar, which owns the project list.
  clientName: string
  projectName: string
  focusIds: string[]
  // Tag ids for a HARD scope query (advisory what-now): a project focus is
  // just that project's tag; a client focus is the client tag PLUS its
  // project tags, so it scopes even before the project list loads and also
  // catches tasks tagged with the client but no project.
  scopeTagIds: string[]
  active: boolean
  setClient: (id: string, name: string) => void
  setProject: (id: string) => void
  setClientProjectIds: (ids: string[]) => void
  setProjectName: (name: string) => void
}

const Ctx = createContext<FocusCtx>({
  clientId: '',
  projectId: '',
  clientName: '',
  projectName: '',
  focusIds: [],
  scopeTagIds: [],
  active: false,
  setClient: () => {},
  setProject: () => {},
  setClientProjectIds: () => {},
  setProjectName: () => {},
})

export function FocusProvider({ children }: { children: ReactNode }) {
  const wsId = useSession()?.workspaceId ?? ''
  // The key is the whole tenancy guarantee: the state below IS one
  // workspace's focus, so it must not outlive that workspace.
  return (
    <FocusState key={wsId} wsId={wsId}>
      {children}
    </FocusState>
  )
}

function FocusState({ wsId, children }: { wsId: string; children: ReactNode }) {
  const [rec, setRec] = useState<Rec>(() => load(wsId))
  const [clientProjectIds, setCPIds] = useState<string[]>([])
  const [projectName, setPName] = useState('')

  const { clientId, clientName, projectId } = rec

  const write = useCallback(
    (next: Rec) => {
      save(wsId, next)
      setRec(next)
    },
    [wsId],
  )
  // The name is written with the id, never separately: the two cannot
  // disagree, and clearing the client clears both.
  const setClient = useCallback(
    (id: string, name: string) =>
      // Changing client invalidates any project narrowing.
      write({ clientId: id, clientName: id ? name : '', projectId: '' }),
    [write],
  )
  const setProject = useCallback(
    (id: string) => write({ ...rec, projectId: id }),
    [write, rec],
  )

  const value = useMemo<FocusCtx>(() => {
    const focusIds = projectId
      ? [projectId]
      : clientId
        ? clientProjectIds
        : []
    const scopeTagIds = projectId
      ? [projectId]
      : clientId
        ? [clientId, ...clientProjectIds]
        : []
    return {
      clientId,
      projectId,
      clientName,
      projectName,
      focusIds,
      scopeTagIds,
      active: !!clientId || !!projectId,
      setClient,
      setProject,
      setClientProjectIds: setCPIds,
      setProjectName: setPName,
    }
  }, [
    clientId,
    projectId,
    clientName,
    projectName,
    clientProjectIds,
    setClient,
    setProject,
  ])

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

// Colocated with the provider on purpose (one small focus module).
// eslint-disable-next-line react-refresh/only-export-components
export function useFocus(): FocusCtx {
  return useContext(Ctx)
}
