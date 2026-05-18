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
  focusIds: string[]
  active: boolean
  setClient: (id: string) => void
  setProject: (id: string) => void
  setClientProjectIds: (ids: string[]) => void
}

const Ctx = createContext<FocusCtx>({
  clientId: '',
  projectId: '',
  focusIds: [],
  active: false,
  setClient: () => {},
  setProject: () => {},
  setClientProjectIds: () => {},
})

export function FocusProvider({ children }: { children: ReactNode }) {
  const [clientId, setCid] = useState(() => localStorage.getItem(CK) ?? '')
  const [projectId, setPid] = useState(() => localStorage.getItem(PK) ?? '')
  const [clientProjectIds, setCPIds] = useState<string[]>([])

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

  const value = useMemo<FocusCtx>(() => {
    const focusIds = projectId
      ? [projectId]
      : clientId
        ? clientProjectIds
        : []
    return {
      clientId,
      projectId,
      focusIds,
      active: !!clientId || !!projectId,
      setClient,
      setProject,
      setClientProjectIds: setCPIds,
    }
  }, [clientId, projectId, clientProjectIds])

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

// Colocated with the provider on purpose (one small focus module).
// eslint-disable-next-line react-refresh/only-export-components
export function useFocus(): FocusCtx {
  return useContext(Ctx)
}
