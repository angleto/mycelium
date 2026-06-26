// Client session: JWT (user identity) + the active workspace id.
// Personal-first (ADR-0024): the workspace is NOT chosen at login; it
// is a per-request context switched in-app with no re-auth. The last
// active workspace is remembered so a returning user lands where they
// left off. Cached so the snapshot ref is stable for
// useSyncExternalStore.

export type Session = {
  token: string
  workspaceId: string
  // Long-lived rotating refresh token (90d server-side default).
  // Optional for backwards compat: a session minted before refresh
  // support shipped has none, and falls back to the legacy "log out
  // on 401" behaviour until the user next logs in.
  refreshToken?: string
}

const KEY = 'mycelium.session'
const LAST_WS = 'mycelium.lastWorkspace'
// Sudo-style admin elevation: an admin account runs as a normal user
// and only acts as admin while this is on (cleared on logout). It is
// purely a client signal; the server re-checks the capability and the
// X-Admin-Mode header on every admin call (costa_associati model).
const ADMIN_MODE = 'mycelium.adminMode'
// Sudo-style WORKSPACE role: you operate at the least privilege
// (member) by default and explicitly switch UP to owner/admin when you
// need to mutate clients/workflows/billing. Client signal only; the
// server clamps X-Workspace-Role to the real membership role (a forged
// higher value cannot escalate). Cleared on logout.
const WS_ROLE = 'mycelium.workspaceRole'
type Listener = () => void
const listeners = new Set<Listener>()

function read(): Session | null {
  const raw = localStorage.getItem(KEY)
  if (!raw) return null
  try {
    const v = JSON.parse(raw) as Partial<Session>
    if (!v.token || !v.workspaceId) return null
    return {
      token: v.token,
      workspaceId: v.workspaceId,
      refreshToken: typeof v.refreshToken === 'string' ? v.refreshToken : undefined,
    }
  } catch {
    return null
  }
}

let cache: Session | null = read()
let adminCache: boolean = localStorage.getItem(ADMIN_MODE) === '1'
// '' = default (member). Otherwise one of owner|admin|member|guest.
let wsRoleCache: string = localStorage.getItem(WS_ROLE) ?? ''

export function getSession(): Session | null {
  return cache
}

export function isAdminMode(): boolean {
  return adminCache
}

export function setAdminMode(on: boolean): void {
  adminCache = on
  if (on) localStorage.setItem(ADMIN_MODE, '1')
  else localStorage.removeItem(ADMIN_MODE)
  emit()
}

// '' means "default" (the server treats absent/blank as member).
export function getWorkspaceRole(): string {
  return wsRoleCache
}

export function setWorkspaceRole(role: string): void {
  wsRoleCache = role
  if (role) localStorage.setItem(WS_ROLE, role)
  else localStorage.removeItem(WS_ROLE)
  emit()
}

export function lastWorkspaceId(): string | null {
  return localStorage.getItem(LAST_WS)
}

function emit(): void {
  for (const l of listeners) l()
}

export function setSession(s: Session): void {
  cache = s
  localStorage.setItem(KEY, JSON.stringify(s))
  localStorage.setItem(LAST_WS, s.workspaceId)
  emit()
}

export function setActiveWorkspace(workspaceId: string): void {
  if (!cache) return
  cache = { ...cache, workspaceId }
  localStorage.setItem(KEY, JSON.stringify(cache))
  localStorage.setItem(LAST_WS, workspaceId)
  emit()
}

/** Rotate the access (+ refresh) token of the current session in
 * place. Used by the /auth/refresh interceptor: keeps workspaceId
 * intact and emits so listeners (header bearer cache, etc.) pick
 * up the new credentials immediately. No-op if no session. */
export function updateSessionTokens(
  token: string,
  refreshToken: string | undefined,
): void {
  if (!cache) return
  cache = { ...cache, token, refreshToken }
  localStorage.setItem(KEY, JSON.stringify(cache))
  emit()
}

export function clearSession(): void {
  cache = null
  adminCache = false
  wsRoleCache = ''
  localStorage.removeItem(KEY)
  localStorage.removeItem(ADMIN_MODE)
  localStorage.removeItem(WS_ROLE)
  emit()
}

// The JWT 'sub' (user id). Read-only client decode (no verification:
// the server enforces it); used where an endpoint needs the user id
// and there is no /auth/me.
export function currentUserId(): string | null {
  const s = cache
  if (!s) return null
  try {
    const part = s.token.split('.')[1]
    const json = atob(part.replace(/-/g, '+').replace(/_/g, '/'))
    const claims = JSON.parse(json) as { sub?: unknown }
    return typeof claims.sub === 'string' ? claims.sub : null
  } catch {
    return null
  }
}

export function subscribe(l: Listener): () => void {
  listeners.add(l)
  return () => {
    listeners.delete(l)
  }
}
