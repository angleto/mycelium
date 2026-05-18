import { Navigate, Outlet } from 'react-router-dom'
import { useSession } from '../auth/useSession'

export function RequireAuth() {
  const session = useSession()
  if (!session) return <Navigate to="/login" replace />
  return <Outlet />
}
