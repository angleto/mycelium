import { useCallback, useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { authFetch, workspaceHeader } from '../api/client'

// A client picker that SEARCHES instead of enumerating.
//
// The set of clients is not bounded: a workspace invoicing through a payment
// connector grows one client per paying customer, so a <select> over them stops
// being usable long before the data does — while a search over the same set
// behaves identically at ten clients and at ten thousand.
//
// Nothing is hidden and nothing needs promoting. Every client is reachable: the
// empty box offers the ones with recent activity (which is the handful anyone
// actually works with), and the rest is one word away. That is deliberately the
// opposite of filtering connector clients out of the list — a customer who pays
// for a consultancy has to be findable the moment you need them.
//
// Debounce and open/close shape follow AssigneePicker, which is the same
// problem solved once already in this codebase.
type ClientRow = { id: string; name: string; vat_number?: string | null }

export function ClientSearch({
  currentName,
  onChange,
  placeholder,
  allLabel,
}: {
  /** Label for the current selection, so the control can render it without
   *  having the row in ``matches`` (the selected client is usually NOT in the
   *  first page of results). */
  currentName: string
  onChange: (id: string, name: string) => void
  placeholder?: string
  /** When given, an explicit "no selection" row (the focus's "all clients"). */
  allLabel?: string
}) {
  const { t } = useTranslation()
  const [q, setQ] = useState('')
  const [open, setOpen] = useState(false)
  const [rows, setRows] = useState<ClientRow[]>([])
  const [busy, setBusy] = useState(false)
  const blurTimer = useRef<number | null>(null)

  const search = useCallback(async (needle: string) => {
    setBusy(true)
    const params = new URLSearchParams({ limit: '20' })
    // No needle: the most recently ACTIVE clients rather than the
    // alphabetically first, which is what makes an empty box useful.
    if (needle) params.set('q', needle)
    else params.set('recent', 'true')
    try {
      const res = await authFetch(`/clients?${params.toString()}`, {
        headers: workspaceHeader() as Record<string, string>,
      })
      setRows(res.ok ? ((await res.json()) as ClientRow[]) : [])
    } catch {
      setRows([])
    }
    setBusy(false)
  }, [])

  useEffect(() => {
    if (!open) return
    const h = window.setTimeout(() => void search(q.trim()), 200)
    return () => window.clearTimeout(h)
  }, [q, open, search])

  useEffect(
    () => () => {
      if (blurTimer.current) window.clearTimeout(blurTimer.current)
    },
    [],
  )

  function pick(id: string, name: string) {
    onChange(id, name)
    setQ('')
    setOpen(false)
  }

  return (
    <span className="csearch">
      <input
        className="csearch__input"
        value={open ? q : currentName}
        placeholder={placeholder ?? t('clientSearch.placeholder')}
        onChange={(e) => setQ(e.target.value)}
        onFocus={() => {
          setQ('')
          setOpen(true)
        }}
        onBlur={() => {
          // Deferred: a click on a row fires blur first, and closing
          // immediately would unmount the row before its handler runs.
          blurTimer.current = window.setTimeout(() => setOpen(false), 150)
        }}
      />
      {open && (
        <ul className="csearch__list">
          {allLabel !== undefined && (
            <li>
              <button type="button" className="csearch__row" onClick={() => pick('', '')}>
                {allLabel}
              </button>
            </li>
          )}
          {busy && rows.length === 0 && (
            <li className="csearch__hint">{t('home.loading')}</li>
          )}
          {!busy && rows.length === 0 && (
            <li className="csearch__hint">{t('clientSearch.none')}</li>
          )}
          {rows.map((c) => (
            <li key={c.id}>
              <button
                type="button"
                className="csearch__row"
                onClick={() => pick(c.id, c.name)}
              >
                ▲ {c.name}
                {c.vat_number && <span className="muted"> · {c.vat_number}</span>}
              </button>
            </li>
          ))}
          {!q && rows.length > 0 && (
            <li className="csearch__hint">{t('clientSearch.recentHint')}</li>
          )}
        </ul>
      )}
    </span>
  )
}
