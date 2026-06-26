import { useCallback, useEffect, useRef } from 'react'

/** Coalescing-window TTL between two keystrokes that should still be
 * treated as part of the same web revision. Matches the server-side
 * COALESCE_WINDOW_SECONDS in mycelium_core.services.entity_revisions —
 * cross both ways so a slow client clock cannot accidentally extend
 * the window past what the server accepts. */
const SESSION_IDLE_GAP_MS = 30_000

/** State carried by ``useEditSession``.
 *
 * The id (UUID) is generated lazily on the first edit and stays
 * stable for as long as keystrokes arrive within
 * ``SESSION_IDLE_GAP_MS``. ``touch()`` is called before every PATCH
 * so the next call inherits the same id; ``seal()`` closes the
 * window explicitly (on blur, route change, Esc/Cmd+S) and the next
 * ``touch()`` will mint a fresh id.
 */
export interface EditSession {
  /** Returns the current edit-session id, generating one if the
   * window had expired or was never opened. Idempotent within the
   * coalescing gap. */
  touch: () => string
  /** Returns the current id WITHOUT extending the window. ``null``
   * if no session is open. Useful for the seal POST body, which
   * must address the id of the just-finished session, not a fresh
   * one. */
  current: () => string | null
  /** Close the current window. Idempotent. Optional callback gets
   * invoked with the id that was sealed, so the caller can fire the
   * ``POST /edit-session/seal`` request without storing the id
   * itself. */
  seal: (notify?: (id: string) => void) => void
}

function makeId(): string {
  // crypto.randomUUID is universally available in modern browsers
  // and in the test renderer. No fallback: an SPA running on a
  // browser too old for it has other problems.
  return crypto.randomUUID()
}

/** Manage a per-entity ``edit_session_id`` for the recovery-history
 * coalescing channel. One hook instance per editable entity (task
 * or note) — never share across entities, since the server keys
 * the open revision on ``(entity_kind, entity_id, channel,
 * edit_session_id)``.
 *
 * The hook also auto-seals on unmount (route change, modal close)
 * so a leaked open revision is unlikely; the worker safety-net
 * job seals anything still open past 60s anyway, so this is
 * defense in depth, not the primary mechanism.
 */
export function useEditSession(onSeal?: (id: string) => void): EditSession {
  const idRef = useRef<string | null>(null)
  const lastTouchRef = useRef<number>(0)
  const onSealRef = useRef(onSeal)
  // Keep the ref in sync with the latest callback after every render.
  // (Storing it in a ref avoids re-creating ``touch``/``seal``/``current``
  // on every parent render, which would invalidate downstream
  // ``useCallback`` deps unnecessarily.)
  useEffect(() => {
    onSealRef.current = onSeal
  }, [onSeal])

  const seal = useCallback((notify?: (id: string) => void) => {
    const id = idRef.current
    if (!id) return
    idRef.current = null
    lastTouchRef.current = 0
    const cb = notify ?? onSealRef.current
    if (cb) cb(id)
  }, [])

  const touch = useCallback((): string => {
    const now = Date.now()
    if (idRef.current && now - lastTouchRef.current <= SESSION_IDLE_GAP_MS) {
      lastTouchRef.current = now
      return idRef.current
    }
    // Either no session, or the previous one is past the idle gap.
    // Seal the dead one (so the caller can POST /edit-session/seal
    // if it tracks it) and mint a fresh id.
    if (idRef.current) {
      seal()
    }
    const id = makeId()
    idRef.current = id
    lastTouchRef.current = now
    return id
  }, [seal])

  const current = useCallback((): string | null => idRef.current, [])

  // Unmount = route change / detail pane close. Auto-seal as a
  // safety net so a navigation away doesn't leave the open
  // revision dangling until the worker's idle timeout.
  useEffect(() => {
    return () => {
      seal()
    }
  }, [seal])

  return { touch, current, seal }
}
