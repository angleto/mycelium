import { useSyncExternalStore } from 'react'
import { getSession, isAdminMode, subscribe } from './session'

export function useSession() {
  return useSyncExternalStore(subscribe, getSession)
}

// Reactive admin-elevation flag. Separate snapshot from useSession
// (toggling elevation does not change the Session ref, so a
// useSession subscriber would not re-render).
export function useAdminMode(): boolean {
  return useSyncExternalStore(subscribe, isAdminMode)
}
