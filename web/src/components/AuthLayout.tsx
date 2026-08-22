import { Outlet } from 'react-router-dom'
import { UpdateBanner } from './UpdateBanner'

// Centres the public auth screens (login/register/verify/forgot/
// reset). These render a bare .card outside the AppShell, so without a
// layout they pinned to the top-left corner.
export function AuthLayout() {
  return (
    <div className="authwrap">
      {/* Also here, not only in AppShell: a logged-out tab left open
          overnight is the likeliest stale bundle of all, and it is the
          one that would meet a changed auth contract on the next login
          attempt. Nothing is unsaved on these screens, so in practice
          the watcher reloads silently and this renders nothing. */}
      <UpdateBanner />
      <Outlet />
    </div>
  )
}
