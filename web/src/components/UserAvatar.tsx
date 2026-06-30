import { useEffect, useRef, useState } from 'react'
import { NavLink } from 'react-router-dom'
import { authFetch } from '../api/client'
import { useMe } from '../auth/useMe'

// The logged-in user's generated mycelium-QR avatar, shown in the topbar /
// user menu (links to Settings). The bytes are bearer-protected, so they are
// fetched as a blob and shown via an object URL (an <img src> straight to the
// endpoint would 401). Refetches on the ``avatar-updated`` window event the
// generator fires after a save, so a new avatar appears without a reload.
export function UserAvatar() {
  const { me } = useMe()
  const hasAvatar = !!me?.has_avatar
  const [url, setUrl] = useState<string | null>(null)
  const urlRef = useRef<string | null>(null)
  const seq = useRef(0)

  // Mirror the current object URL into a ref so the unmount cleanup can revoke
  // it without depending on (or setting) render state.
  useEffect(() => {
    urlRef.current = url
  }, [url])

  useEffect(() => {
    if (!hasAvatar) return
    let cancelled = false
    const s = ++seq.current
    const fetchAvatar = async () => {
      const res = await authFetch('/auth/me/avatar')
      if (cancelled || s !== seq.current || !res.ok) return
      const blob = await res.blob()
      if (cancelled || s !== seq.current) return
      const next = URL.createObjectURL(blob)
      setUrl((prev) => {
        if (prev) URL.revokeObjectURL(prev)
        return next
      })
    }
    void fetchAvatar()
    const onUpdated = () => void fetchAvatar()
    window.addEventListener('avatar-updated', onUpdated)
    return () => {
      cancelled = true
      window.removeEventListener('avatar-updated', onUpdated)
    }
  }, [hasAvatar])

  // Revoke the last object URL on unmount (via a ref, so no setState here).
  useEffect(
    () => () => {
      if (urlRef.current) URL.revokeObjectURL(urlRef.current)
    },
    [],
  )

  if (!hasAvatar || !url) return null
  const label = me?.display_name || me?.email || ''
  return (
    <NavLink to="/settings" title={label} aria-label={label || 'avatar'}>
      <img
        src={url}
        alt=""
        width={28}
        height={28}
        style={{ borderRadius: '50%', objectFit: 'cover', display: 'block' }}
      />
    </NavLink>
  )
}
