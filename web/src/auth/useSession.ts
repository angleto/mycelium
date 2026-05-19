import { useSyncExternalStore } from 'react'
import {
  getSession,
  getWorkspaceRole,
  isAdminMode,
  subscribe,
} from './session'

export function useSession() {
  return useSyncExternalStore(subscribe, getSession)
}

// Reactive admin-elevation flag. Separate snapshot from useSession
// (toggling elevation does not change the Session ref, so a
// useSession subscriber would not re-render).
export function useAdminMode(): boolean {
  return useSyncExternalStore(subscribe, isAdminMode)
}

// Reactive effective workspace role ('' = default/member).
export function useWorkspaceRole(): string {
  return useSyncExternalStore(subscribe, getWorkspaceRole)
}
