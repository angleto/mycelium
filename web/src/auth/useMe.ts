import { useEffect, useState } from 'react'
import { api } from '../api/client'
import type { components } from '../api/schema'
import { useSession } from './useSession'

export type Me = components['schemas']['MeOut']

// Canonical identity (server-checked is_admin capability). Cached per
// token so a re-render or a second consumer does not refetch; the
// elevation *mode* lives in session (client signal), this is only the
// capability + identity. Derived in render (no setState in the
// effect): the effect only writes state *after* the await.
let cache: { token: string; me: Me } | null = null

export function useMe(): { me: Me | null; loading: boolean } {
  const session = useSession()
  const token = session?.token ?? null
  const [fetched, setFetched] = useState<{ token: string; me: Me } | null>(
    cache,
  )

  useEffect(() => {
    if (!token) return
    if (cache && cache.token === token) return
    let active = true
    void (async () => {
      const { data } = await api.GET('/auth/me')
      if (!active || !data) return
      cache = { token, me: data }
      setFetched(cache)
    })()
    return () => {
      active = false
    }
  }, [token])

  const me =
    token && cache && cache.token === token
      ? cache.me
      : token && fetched && fetched.token === token
        ? fetched.me
        : null
  return { me, loading: !!token && me === null }
}
