import { Navigate, Outlet } from 'react-router-dom'
import { useSession } from '../auth/useSession'

export function RequireAuth() {
  const session = useSession()
  if (!session) return <Navigate to="/login" replace />
  // Keyed by workspace on purpose (task 805a569c). Switching workspace is an
  // in-app context switch, not a reload (auth/session.setActiveWorkspace only
  // rewrites the session and emits), so without this every component below
  // keeps the previous tenant's fetched rows -- project lists, tag catalogues,
  // client-search results -- until its own refetch lands, and indefinitely if
  // that refetch fails. Remounting the authenticated subtree makes tenant data
  // outliving its tenant unrepresentable, instead of leaving each component to
  // remember to clear itself.
  return <Outlet key={session.workspaceId} />
}
