import { Outlet } from 'react-router-dom'

// Centres the public auth screens (login/register/verify/forgot/
// reset). These render a bare .card outside the AppShell, so without a
// layout they pinned to the top-left corner.
export function AuthLayout() {
  return (
    <div className="authwrap">
      <Outlet />
    </div>
  )
}
