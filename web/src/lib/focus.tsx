import {
  createContext,
  useContext,
  useState,
  type ReactNode,
} from 'react'

// Project focus: a single selected project (chosen in the sidebar)
// that the project-scoped views (Notes, Tasks) filter to, so working
// on one project is distraction-free and low-click. Empty = no focus
// (everything). Persisted in localStorage, per browser.
const KEY = 'flow-focus-project'

type FocusCtx = {
  projectId: string
  setProjectId: (id: string) => void
}

const Ctx = createContext<FocusCtx>({
  projectId: '',
  setProjectId: () => {},
})

export function FocusProvider({ children }: { children: ReactNode }) {
  const [projectId, setPid] = useState(
    () => localStorage.getItem(KEY) ?? '',
  )
  const setProjectId = (id: string) => {
    if (id) localStorage.setItem(KEY, id)
    else localStorage.removeItem(KEY)
    setPid(id)
  }
  return (
    <Ctx.Provider value={{ projectId, setProjectId }}>
      {children}
    </Ctx.Provider>
  )
}

// Colocated with the provider on purpose (one small focus module).
// eslint-disable-next-line react-refresh/only-export-components
export function useFocus(): FocusCtx {
  return useContext(Ctx)
}
