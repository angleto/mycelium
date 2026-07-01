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
    let active = true
    const load = async () => {
      const { data } = await api.GET('/auth/me')
      if (!active || !data) return
      cache = { token, me: data }
      setFetched(cache)
    }
    if (!cache || cache.token !== token) void load()
    // After the user saves a new avatar, the cached me.avatar_* is stale;
    // refetch so every consumer (topbar, the issuer logo that reuses the
    // avatar) picks up the new seed/colours without a full reload.
    const onAvatarUpdated = () => {
      cache = null
      void load()
    }
    window.addEventListener('avatar-updated', onAvatarUpdated)
    return () => {
      active = false
      window.removeEventListener('avatar-updated', onAvatarUpdated)
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
