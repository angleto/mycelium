import { useSyncExternalStore } from 'react'
import { getSession, subscribe } from './session'

export function useSession() {
  return useSyncExternalStore(subscribe, getSession)
}
