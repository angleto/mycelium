import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { authFetch, workspaceHeader } from '../api/client'

// docs/adr/0028 D5: owner is the accountability axis on a task and
// must always be a real user (never an ai_assistant). This picker
// queries /actors?q= filtered to ``kind=user`` and surfaces both the
// owning user's display name and its uuid (sent to the API as
// ``owner_id``). Single-select; no clear (a task always has an
// owner -- transfer, never unassign).

type Actor = {
  handle: string
  kind: 'user' | 'ai_assistant'
  display_name: string
  ref_id: string
}

export function OwnerPicker({
  value,
  onChange,
  disabled,
}: {
  value: string | null
  onChange: (nextOwnerId: string) => void
  disabled?: boolean
}) {
  const { t } = useTranslation()
  const [q, setQ] = useState('')
  const [open, setOpen] = useState(false)
  const [matches, setMatches] = useState<Actor[]>([])
  const [busy, setBusy] = useState(false)
  const [allUsers, setAllUsers] = useState<Actor[]>([])
  const blurTimer = useRef<number | null>(null)

  const search = useCallback(async (needle: string) => {
    setBusy(true)
    const params = new URLSearchParams()
    if (needle) params.set('q', needle)
    params.set('limit', '25')
    const url = `/actors?${params.toString()}`
    const res = await authFetch(url, {
      headers: workspaceHeader() as Record<string, string>,
    })
    if (res.ok) {
      const data = (await res.json()) as Actor[]
      setMatches(data.filter((a) => a.kind === 'user'))
    } else {
      setMatches([])
    }
    setBusy(false)
  }, [])

  // Resolve the current owner_id to a display name once on mount:
  // /actors returns handle + display_name + ref_id (= user.id for
  // kind=user). We cache the lookup in ``allUsers`` so subsequent
  // edits don't re-query for the same owner.
  useEffect(() => {
    let active = true
    void (async () => {
      // include_inactive: this fetch is not the picker (that one is
      // ``search`` above, and it must stay clean) -- it is the ONLY
      // source the SPA has for owner_id -> name, and its miss path
      // renders eight hex characters of a raw uuid. Deactivating
      // someone must not turn every task they own into a uuid.
      const res = await authFetch('/actors?limit=200&include_inactive=true', {
        headers: workspaceHeader() as Record<string, string>,
      })
      if (!active) return
      if (res.ok) {
        const data = (await res.json()) as Actor[]
        setAllUsers(data.filter((a) => a.kind === 'user'))
      }
    })()
    return () => {
      active = false
    }
  }, [])

  useEffect(() => {
    if (!open) return
    const h = window.setTimeout(() => void search(q.trim()), 200)
    return () => window.clearTimeout(h)
  }, [q, open, search])

  const currentLabel = useMemo(() => {
    if (!value) return ''
    const hit = allUsers.find((a) => a.ref_id === value)
    return hit ? `${hit.display_name} (@${hit.handle})` : value.slice(0, 8)
  }, [value, allUsers])

  function pick(actor: Actor) {
    onChange(actor.ref_id)
    setQ('')
    setOpen(false)
  }

  return (
    <div className="assignpick">
      <div className="row assignpick__row">
        {value ? (
          <span className="chip chip--owner assignpick__current" title={currentLabel}>
            <span className="chip__glyph" aria-hidden="true">
              ●
            </span>
            {currentLabel}
          </span>
        ) : (
          <span className="hint">{t('ownerPicker.none')}</span>
        )}
      </div>
      {!disabled && (
        <div className="assignpick__searchbox">
          <input
            className="assignpick__search"
            placeholder={t('ownerPicker.search')}
            value={q}
            onFocus={() => {
              if (blurTimer.current != null) {
                window.clearTimeout(blurTimer.current)
                blurTimer.current = null
              }
              setOpen(true)
            }}
            onBlur={() => {
              blurTimer.current = window.setTimeout(() => setOpen(false), 150)
            }}
            onChange={(e) => setQ(e.target.value)}
          />
          {open && (
            <ul className="assignpick__list">
              {busy && <li className="hint assignpick__hint">…</li>}
              {!busy && matches.length === 0 && (
                <li className="hint assignpick__hint">{t('ownerPicker.noMatch')}</li>
              )}
              {matches.map((a) => (
                <li key={`user-${a.ref_id}`}>
                  <button
                    type="button"
                    className="assignpick__opt"
                    onMouseDown={(e) => e.preventDefault()}
                    onClick={() => pick(a)}
                  >
                    <span className="chip__glyph" aria-hidden="true">
                      ●
                    </span>
                    <span className="assignpick__handle">@{a.handle}</span>
                    <span className="muted">{a.display_name}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  )
}
