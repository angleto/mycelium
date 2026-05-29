import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api, workspaceHeader } from '../api/client'
import {
  isPrefixCandidate,
  lookupPrefix,
  type LookupMatch,
} from '../lib/prefixLookup'

// Global Cmd/Ctrl+K palette: "go to / search". It closes the
// discoverability gap ADR-0038 deferred (analysis item D) — the whole
// point being that typing a task/note CODE (the 8-char UUID prefix
// pervasive in roadmap notes) actually FINDS the entity, which the
// /tasks and /notes free-text search boxes never did (they match
// title/body text, never the id column).
//
// Two result sources, merged:
//   * id branch — when the query looks like a hex prefix, the
//     deterministic /lookup resolver returns the matching task/note(s),
//     shown first with an ``id`` badge.
//   * text branch — substring match over task / note titles (same
//     lightweight client-side filter the editor's @-mention typeahead
//     uses), so the palette is also a plain title search.
//
// Navigation uses the server-supplied route_url for id matches and the
// canonical /tasks/:id /notes/:id routes for title matches.

interface Row {
  key: string
  kind: 'task' | 'note'
  title: string
  route: string
  byId: boolean
  sub?: string
}

const GLYPH: Record<'task' | 'note', string> = { task: '✓', note: '◆' }

export function CommandPalette() {
  const navigate = useNavigate()
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  const [q, setQ] = useState('')
  const [sel, setSel] = useState(0)
  const [tasks, setTasks] = useState<{ id: string; title: string }[]>([])
  const [notes, setNotes] = useState<{ id: string; title: string }[]>([])
  // id matches are kept keyed by the prefix they were resolved for, so a
  // stale or non-prefix query simply stops contributing rows (gated in
  // the memo) without a synchronous clear inside an effect.
  const [idLookup, setIdLookup] = useState<{
    prefix: string
    matches: LookupMatch[]
  }>({ prefix: '', matches: [] })
  const inputRef = useRef<HTMLInputElement>(null)

  // Global hotkey: Cmd/Ctrl+K toggles, Escape closes. setState happens
  // in the listener callback, not synchronously in the effect body.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && (e.key === 'k' || e.key === 'K')) {
        e.preventDefault()
        setOpen((v) => !v)
      } else if (e.key === 'Escape' && open) {
        setOpen(false)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open])

  // Reset query/selection when the palette toggles. Render-time
  // derive-from-prop reset (the idiom used in PrefixResolver) rather
  // than a setState-in-effect.
  const [lastOpen, setLastOpen] = useState(open)
  if (lastOpen !== open) {
    setLastOpen(open)
    setQ('')
    setSel(0)
    setIdLookup({ prefix: '', matches: [] })
  }

  // Load the title-search corpus once per open (async setState in the
  // promise callback) and focus the input.
  useEffect(() => {
    if (!open) return
    let active = true
    void (async () => {
      const h = workspaceHeader()
      const [tk, nt] = await Promise.all([
        api.GET('/tasks', { params: { header: h } }),
        api.GET('/notes', { params: { header: h } }),
      ])
      if (!active) return
      setTasks((tk.data ?? []).map((x) => ({ id: x.id, title: x.title })))
      setNotes(
        (nt.data ?? []).map((x) => ({ id: x.id, title: x.title ?? x.kind })),
      )
    })()
    requestAnimationFrame(() => inputRef.current?.focus())
    return () => {
      active = false
    }
  }, [open])

  // Resolve the id branch as the user types a code (async setState).
  useEffect(() => {
    const s = q.trim().toLowerCase()
    if (!isPrefixCandidate(s)) return
    let active = true
    void lookupPrefix(s, { kinds: ['task', 'note'] }).then((res) => {
      if (active) setIdLookup({ prefix: s, matches: res?.matches ?? [] })
    })
    return () => {
      active = false
    }
  }, [q])

  const results = useMemo<Row[]>(() => {
    const needle = q.trim().toLowerCase()
    const out: Row[] = []
    if (needle && idLookup.prefix === needle) {
      for (const m of idLookup.matches) {
        out.push({
          key: `id-${m.kind}-${m.id}`,
          kind: m.kind,
          title: m.title?.trim() || m.id,
          route: m.route_url,
          byId: true,
          sub: m.kind === 'task' ? (m.state_name ?? 'task') : 'note',
        })
      }
    }
    if (needle) {
      for (const tk of tasks) {
        if (tk.title.toLowerCase().includes(needle)) {
          out.push({
            key: `t-${tk.id}`,
            kind: 'task',
            title: tk.title,
            route: `/tasks/${tk.id}`,
            byId: false,
          })
        }
      }
      for (const n of notes) {
        if (n.title.toLowerCase().includes(needle)) {
          out.push({
            key: `n-${n.id}`,
            kind: 'note',
            title: n.title,
            route: `/notes/${n.id}`,
            byId: false,
          })
        }
      }
    }
    const seen = new Set<string>()
    return out
      .filter((r) => (seen.has(r.route) ? false : (seen.add(r.route), true)))
      .slice(0, 12)
  }, [q, idLookup, tasks, notes])

  if (!open) return null

  // Clamp the highlight to the live result set instead of resetting it
  // from an effect.
  const active = results.length ? Math.min(sel, results.length - 1) : 0

  const go = (r: Row) => {
    setOpen(false)
    navigate(r.route)
  }

  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setSel(results.length ? (active + 1) % results.length : 0)
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setSel(results.length ? (active - 1 + results.length) % results.length : 0)
    } else if (e.key === 'Enter') {
      e.preventDefault()
      const r = results[active]
      if (r) go(r)
    }
  }

  return (
    <div
      className="cmdk"
      role="dialog"
      aria-modal="true"
      aria-label={t('cmdk.title')}
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) setOpen(false)
      }}
    >
      <div className="cmdk__box">
        <input
          ref={inputRef}
          className="cmdk__input"
          value={q}
          onChange={(e) => {
            setQ(e.target.value)
            setSel(0)
          }}
          onKeyDown={onKeyDown}
          placeholder={t('cmdk.placeholder')}
          aria-label={t('cmdk.placeholder')}
          aria-activedescendant={results[active] ? `cmdk-opt-${active}` : undefined}
        />
        <ul className="cmdk__list" role="listbox">
          {results.map((r, i) => (
            <li
              key={r.key}
              id={`cmdk-opt-${i}`}
              role="option"
              aria-selected={i === active}
              className={'cmdk__row' + (i === active ? ' cmdk__row--sel' : '')}
              onMouseEnter={() => setSel(i)}
              onMouseDown={(e) => {
                e.preventDefault()
                go(r)
              }}
            >
              <span className="cmdk__glyph" aria-hidden="true">
                {GLYPH[r.kind]}
              </span>
              <span className="cmdk__title">{r.title}</span>
              {r.byId && (
                <span className="cmdk__badge" aria-label="matched by id">
                  id
                </span>
              )}
              {r.sub && <span className="cmdk__sub">{r.sub}</span>}
            </li>
          ))}
          {results.length === 0 && (
            <li className="cmdk__empty">
              {q.trim() ? t('cmdk.noResults') : t('cmdk.hint')}
            </li>
          )}
        </ul>
      </div>
    </div>
  )
}
