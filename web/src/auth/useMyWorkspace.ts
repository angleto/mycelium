import { useEffect, useState } from 'react'
import { api, workspaceHeader } from '../api/client'
import type { components } from '../api/schema'
import { useSession } from './useSession'

export type Ws = components['schemas']['WorkspaceOut']

// The active workspace incl. the caller's membership role (`my_role`,
// the entitlement ceiling for the role switcher). Cached per active
// workspace id. Derived in render — the effect only writes state
// AFTER the await (no setState-in-effect cascade).
let cache: { wsId: string; ws: Ws } | null = null

export function useMyWorkspace(): {
  ws: Ws | null
  reload: () => Promise<void>
} {
  const session = useSession()
  const wsId = session?.workspaceId ?? null
  const [fetched, setFetched] = useState<{ wsId: string; ws: Ws } | null>(
    cache,
  )

  useEffect(() => {
    if (!wsId) return
    if (cache && cache.wsId === wsId) return
    let active = true
    void (async () => {
      const { data } = await api.GET('/workspaces/me', {
        params: { header: workspaceHeader() },
      })
      if (!active || !data) return
      cache = { wsId, ws: data }
      setFetched(cache)
    })()
    return () => {
      active = false
    }
  }, [wsId])

  const ws =
    wsId && cache && cache.wsId === wsId
      ? cache.ws
      : wsId && fetched && fetched.wsId === wsId
        ? fetched.ws
        : null

  async function reload() {
    if (!wsId) return
    const { data } = await api.GET('/workspaces/me', {
      params: { header: workspaceHeader() },
    })
    if (data) {
      cache = { wsId, ws: data }
      setFetched(cache)
    }
  }

  return { ws, reload }
}
