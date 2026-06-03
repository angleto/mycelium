import {
  createContext,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from 'react'

// Focus mode: narrow the project-scoped views (Notes, Tasks) to one
// CLIENT (all its projects) and, optionally, to a single project
// within it. Distraction-free, low-click. Persisted per browser.
//
// The sidebar owns the client→projects data, so it pushes the
// selected client's project-tag ids via setClientProjectIds; consumers
// only read `focusIds` (the effective project-tag-id allow-list) and
// `active`.
const CK = 'flow-focus-client'
const PK = 'flow-focus-project'

type FocusCtx = {
  clientId: string
  projectId: string
  // Human-readable names of the active focus, for surfacing what the
  // current scope IS (e.g. the advisory "Scoped to …" chip). Pushed by the
  // sidebar picker, which owns the client/project lists.
  clientName: string
  projectName: string
  focusIds: string[]
  // Tag ids for a HARD scope query (advisory what-now): a project focus is
  // just that project's tag; a client focus is the client tag PLUS its
  // project tags, so it scopes even before the project list loads and also
  // catches tasks tagged with the client but no project.
  scopeTagIds: string[]
  active: boolean
  setClient: (id: string) => void
  setProject: (id: string) => void
  setClientProjectIds: (ids: string[]) => void
  setNames: (clientName: string, projectName: string) => void
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
  setNames: () => {},
})

export function FocusProvider({ children }: { children: ReactNode }) {
  const [clientId, setCid] = useState(() => localStorage.getItem(CK) ?? '')
  const [projectId, setPid] = useState(() => localStorage.getItem(PK) ?? '')
  const [clientProjectIds, setCPIds] = useState<string[]>([])
  const [clientName, setCName] = useState('')
  const [projectName, setPName] = useState('')

  const setClient = (id: string) => {
    if (id) localStorage.setItem(CK, id)
    else localStorage.removeItem(CK)
    // Changing client invalidates any project narrowing.
    localStorage.removeItem(PK)
    setCid(id)
    setPid('')
  }
  const setProject = (id: string) => {
    if (id) localStorage.setItem(PK, id)
    else localStorage.removeItem(PK)
    setPid(id)
  }
  const setNames = (c: string, p: string) => {
    setCName(c)
    setPName(p)
  }

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
      setNames,
    }
  }, [clientId, projectId, clientProjectIds, clientName, projectName])

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

// Colocated with the provider on purpose (one small focus module).
// eslint-disable-next-line react-refresh/only-export-components
export function useFocus(): FocusCtx {
  return useContext(Ctx)
}
