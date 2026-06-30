import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { authFetch, workspaceHeader } from '../api/client'

// ActorOut from the /actors endpoint (added in v1.2.23 #21 stage A).
// Inlined because the local dev's openapi gen didn't see /actors when
// schema.d.ts was last regenerated. Will move to schema.d.ts on the
// next ``pnpm gen:api`` run from a fresh uvicorn.
type Actor = {
  handle: string
  kind: 'user' | 'ai_assistant'
  display_name: string
  ref_id: string
}

// Handle-based assignee picker (#21 Stage B). Queries /actors?q= with
// a 200ms debounce, lists matches as ``@handle`` rows with kind glyph
// (▲ user, ◆ ai_assistant — same convention as TagChip). The current
// value is the task's ``assignee_handle`` (a string), not a UUID, so
// the parent does ``onChange('@handle' | '')`` and PATCHes the task.
//
// Mirrors TagPicker's two-pane shape (selected + search) but for a
// single-select scalar field.
export function AssigneePicker({
  value,
  onChange,
  disabled,
}: {
  value: string | null
  onChange: (next: string | null) => void
  disabled?: boolean
}) {
  const { t } = useTranslation()
  const [q, setQ] = useState('')
  const [open, setOpen] = useState(false)
  const [matches, setMatches] = useState<Actor[]>([])
  const [busy, setBusy] = useState(false)
  const blurTimer = useRef<number | null>(null)

  // Holds the actor for ``value`` when we had to fetch it outside the
  // picker's search flow (e.g. on initial mount). Kept separate from
  // ``matches`` so the chip-resolution effect doesn't fight the
  // search effect for the same slot.
  const [fetchedActor, setFetchedActor] = useState<Actor | null>(null)

  const search = useCallback(async (needle: string) => {
    setBusy(true)
    const params = new URLSearchParams()
    if (needle) params.set('q', needle)
    params.set('limit', '25')
    const qs = params.toString()
    const url = `/actors${qs ? `?${qs}` : ''}`
    const res = await authFetch(url, {
      headers: workspaceHeader() as Record<string, string>,
    })
    if (res.ok) {
      const data = (await res.json()) as Actor[]
      setMatches(data)
    } else {
      setMatches([])
    }
    setBusy(false)
  }, [])

  useEffect(() => {
    if (!open) return
    const h = window.setTimeout(() => void search(q.trim()), 200)
    return () => window.clearTimeout(h)
  }, [q, open, search])

  // Resolve the chip's display_name on mount and whenever ``value``
  // changes to a handle we don't have in ``matches``. The bare-handle
  // query against /actors?q= is the most specific lookup we can issue
  // without a per-actor GET endpoint. Stays cheap because it only
  // fires when matches don't cover the current value.
  const matchedActor = useMemo(
    () => (value ? matches.find((a) => a.handle === value) : null) ?? null,
    [value, matches],
  )
  const needsFetch = !!value && !matchedActor && fetchedActor?.handle !== value
  useEffect(() => {
    if (!needsFetch || !value) return
    let cancelled = false
    void (async () => {
      const res = await authFetch(
        `/actors?q=${encodeURIComponent(value)}&limit=5`,
        { headers: workspaceHeader() as Record<string, string> },
      )
      if (!res.ok || cancelled) return
      const data = (await res.json()) as Actor[]
      const exact = data.find((a) => a.handle === value)
      if (exact) setFetchedActor(exact)
    })()
    return () => {
      cancelled = true
    }
  }, [needsFetch, value])

  const currentActor: Actor | null =
    matchedActor ?? (fetchedActor?.handle === value ? fetchedActor : null)
  const currentLabel = useMemo(() => {
    if (!value) return ''
    return currentActor
      ? `${currentActor.display_name} (@${value})`
      : `@${value}`
  }, [value, currentActor])

  function pick(actor: Actor) {
    onChange(actor.handle)
    setQ('')
    setOpen(false)
  }

  function clear() {
    onChange(null)
  }

  return (
    <div className="assignpick">
      <div className="row assignpick__row">
        {value ? (
          <span
            className="chip chip--rm assignpick__current"
            title={currentLabel}
          >
            <span className="chip__glyph" aria-hidden="true">
              {value.startsWith('_a_') ? '◆' : '▲'}
            </span>
            {currentActor && (
              <span className="assignpick__name">{currentActor.display_name}</span>
            )}
            <span className="muted">@{value}</span>
            {!disabled && (
              <button
                type="button"
                className="assignpick__clear"
                title={t('assigneePicker.clear')}
                onClick={clear}
              >
                ✕
              </button>
            )}
          </span>
        ) : (
          <span className="hint">{t('assigneePicker.none')}</span>
        )}
      </div>
      <div className="assignpick__searchbox">
        <input
          className="assignpick__search"
          placeholder={t('assigneePicker.search')}
          value={q}
          disabled={disabled}
          onFocus={() => {
            if (blurTimer.current != null) {
              window.clearTimeout(blurTimer.current)
              blurTimer.current = null
            }
            setOpen(true)
          }}
          onBlur={() => {
            // Defer close so a click on a result registers before the
            // input loses focus and the list unmounts.
            blurTimer.current = window.setTimeout(() => setOpen(false), 150)
          }}
          onChange={(e) => setQ(e.target.value)}
        />
        {open && (
          <ul className="assignpick__list">
            {busy && <li className="hint assignpick__hint">…</li>}
            {!busy && matches.length === 0 && (
              <li className="hint assignpick__hint">
                {t('assigneePicker.noMatch')}
              </li>
            )}
            {matches.map((a) => (
              <li key={`${a.kind}-${a.ref_id}`}>
                <button
                  type="button"
                  className="assignpick__opt"
                  onMouseDown={(e) => e.preventDefault()}
                  onClick={() => pick(a)}
                >
                  <span className="chip__glyph" aria-hidden="true">
                    {a.kind === 'user' ? '▲' : '◆'}
                  </span>
                  <span className="assignpick__handle">@{a.handle}</span>
                  <span className="muted">{a.display_name}</span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}
