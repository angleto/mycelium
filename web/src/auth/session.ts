// Client-side session: JWT token + the tenant org id. X-Org-Id is
// per-request tenant scoping (not a credential), carried explicitly on
// typed calls; the token is injected as the Authorization bearer by the
// API client middleware. Cached so the snapshot reference is stable for
// useSyncExternalStore.

export type Session = { token: string; orgId: string }

const KEY = 'flow.session'
type Listener = () => void
const listeners = new Set<Listener>()

function read(): Session | null {
  const raw = localStorage.getItem(KEY)
  if (!raw) return null
  try {
    const v = JSON.parse(raw) as Partial<Session>
    return v.token && v.orgId ? { token: v.token, orgId: v.orgId } : null
  } catch {
    return null
  }
}

let cache: Session | null = read()

export function getSession(): Session | null {
  return cache
}

function emit(): void {
  for (const l of listeners) l()
}

export function setSession(s: Session): void {
  cache = s
  localStorage.setItem(KEY, JSON.stringify(s))
  emit()
}

export function clearSession(): void {
  cache = null
  localStorage.removeItem(KEY)
  emit()
}

export function subscribe(l: Listener): () => void {
  listeners.add(l)
  return () => {
    listeners.delete(l)
  }
}
