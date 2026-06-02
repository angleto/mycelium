import { Fragment, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api, searchTasksByText, workspaceHeader } from '../api/client'
import {
  isPrefixCandidate,
  lookupPrefix,
  type LookupMatch,
} from '../lib/prefixLookup'
import { getRecents, pushRecent } from '../lib/recents'

// Global Cmd/Ctrl+K palette: "go to / search". It closes the
// discoverability gap ADR-0038 deferred (analysis item D) — the whole
// point being that typing a task/note CODE (the 8-char UUID prefix
// pervasive in roadmap notes) actually FINDS the entity, which the
// /tasks and /notes free-text search boxes never did (they match
// title/body text, never the id column).
//
// Result sources, merged and de-duped by route, grouped into sections:
//   * id branch — when the query looks like a hex prefix, the
//     deterministic /lookup resolver returns the matching task/note(s),
//     shown first with the matched prefix highlighted in the code badge.
//   * server branch (tasks) — POST /search (FTS + pgvector RRF) so a
//     task matched only by description / checklist text / semantics
//     surfaces, not just by title. Debounced + abortable.
//   * client branch — instant substring match over the task / note
//     titles already loaded, so the palette feels live before the
//     server responds and works for note titles (see below).
//   * recent — when the box is empty, the recently-visited entities.
//
// Notes are intentionally NOT server-searched: /search returns note
// matches as memory-blob ids and the SearchHit shape exposes no note
// route, so a server note hit isn't navigable from here. Note titles
// stay on the instant client-side filter; server-side note search is a
// backend follow-up (expose the note id/route on note hits).
//
// Navigation uses the server-supplied route_url for id matches and the
// canonical /tasks/:id /notes/:id routes for the rest.

type Section = 'recent' | 'task' | 'note'

interface Row {
  key: string
  kind: 'task' | 'note'
  id: string
  section: Section
  title: string
  route: string
  // For id-branch rows: the entity's 8-char code and how many leading
  // chars the typed prefix matched (highlighted).
  code?: string
  matchedLen?: number
  sub?: string
}

const GLYPH: Record<'task' | 'note', string> = { task: '✓', note: '◆' }
const SECTION_ORDER: Section[] = ['recent', 'task', 'note']
const TEXT_MIN = 2

// Split a title into nodes with the matched substring wrapped in
// <mark>. Case-insensitive, all occurrences. Returns the plain string
// when there is nothing to highlight (empty needle / no match).
function highlight(title: string, needle: string): React.ReactNode {
  const n = needle.trim().toLowerCase()
  if (!n) return title
  const lo = title.toLowerCase()
  if (!lo.includes(n)) return title
  const out: React.ReactNode[] = []
  let i = 0
  let k = 0
  for (;;) {
    const idx = lo.indexOf(n, i)
    if (idx === -1) {
      out.push(title.slice(i))
      break
    }
    if (idx > i) out.push(title.slice(i, idx))
    out.push(
      <mark key={k++} className="cmdk__hl">
        {title.slice(idx, idx + n.length)}
      </mark>,
    )
    i = idx + n.length
  }
  return out
}

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
  // Server-side TASK hits, keyed by the query string they resolved for
  // (same staleness guard as idLookup).
  const [serverTasks, setServerTasks] = useState<{
    q: string
    hits: { id: string; title: string }[]
  }>({ q: '', hits: [] })
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
    setServerTasks({ q: '', hits: [] })
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

  // Server-side task search (debounced + abortable). Augments the
  // instant client-side title filter with the FTS + pgvector pipeline,
  // so a task matched by description / checklist / semantics surfaces.
  useEffect(() => {
    const needle = q.trim()
    if (needle.length < TEXT_MIN) return
    const ac = new AbortController()
    const handle = window.setTimeout(() => {
      void searchTasksByText(needle, ac.signal)
        .then((hits) => {
          if (ac.signal.aborted) return
          setServerTasks({
            q: needle.toLowerCase(),
            hits: hits
              .filter((h) => h.task_id)
              .map((h) => ({ id: h.task_id as string, title: h.title ?? '' })),
          })
        })
        .catch(() => {
          /* non-2xx / abort: client-side filter still applies */
        })
    }, 220)
    return () => {
      ac.abort()
      window.clearTimeout(handle)
    }
  }, [q])

  const rows = useMemo<Row[]>(() => {
    const needle = q.trim().toLowerCase()
    const collected: Row[] = []
    const seen = new Set<string>()
    const add = (r: Row) => {
      if (seen.has(r.route)) return
      seen.add(r.route)
      collected.push(r)
    }

    if (!needle) {
      for (const r of getRecents()) {
        add({
          key: `recent-${r.route}`,
          kind: r.kind,
          id: r.id,
          section: 'recent',
          title: r.title,
          route: r.route,
        })
      }
    } else {
      // id branch first (deterministic resolve of a pasted code).
      if (idLookup.prefix === needle) {
        const bare = needle.replace(/-/g, '')
        for (const m of idLookup.matches) {
          add({
            key: `id-${m.kind}-${m.id}`,
            kind: m.kind,
            id: m.id,
            section: m.kind,
            title: m.title?.trim() || m.id,
            route: m.route_url,
            code: m.id.replace(/-/g, '').slice(0, 8),
            matchedLen: Math.min(bare.length, 8),
            sub: m.kind === 'task' ? (m.state_name ?? 'task') : 'note',
          })
        }
      }
      // Server-side task hits, only while fresh for this query.
      if (serverTasks.q === needle) {
        for (const h of serverTasks.hits) {
          add({
            key: `s-${h.id}`,
            kind: 'task',
            id: h.id,
            section: 'task',
            title: h.title || h.id,
            route: `/tasks/${h.id}`,
          })
        }
      }
      // Instant client-side title filters (tasks + notes).
      for (const tk of tasks) {
        if (tk.title.toLowerCase().includes(needle)) {
          add({
            key: `t-${tk.id}`,
            kind: 'task',
            id: tk.id,
            section: 'task',
            title: tk.title,
            route: `/tasks/${tk.id}`,
          })
        }
      }
      for (const n of notes) {
        if (n.title.toLowerCase().includes(needle)) {
          add({
            key: `n-${n.id}`,
            kind: 'note',
            id: n.id,
            section: 'note',
            title: n.title,
            route: `/notes/${n.id}`,
          })
        }
      }
    }

    // Group by section (stable within each), cap the total.
    const ordered = SECTION_ORDER.flatMap((s) =>
      collected.filter((r) => r.section === s),
    )
    return ordered.slice(0, 20)
  }, [q, idLookup, serverTasks, tasks, notes])

  if (!open) return null

  // Clamp the highlight to the live result set instead of resetting it
  // from an effect.
  const active = rows.length ? Math.min(sel, rows.length - 1) : 0
  const needle = q.trim()

  const go = (r: Row) => {
    setOpen(false)
    pushRecent({ kind: r.kind, id: r.id, title: r.title, route: r.route })
    navigate(r.route)
  }

  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setSel(rows.length ? (active + 1) % rows.length : 0)
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setSel(rows.length ? (active - 1 + rows.length) % rows.length : 0)
    } else if (e.key === 'Enter') {
      e.preventDefault()
      const r = rows[active]
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
          aria-activedescendant={rows[active] ? `cmdk-opt-${active}` : undefined}
        />
        <ul className="cmdk__list" role="listbox">
          {rows.map((r, i) => {
            // First row of each section gets a header. Pure: derived
            // from the previous row, no mutable render-time variable.
            const header =
              i === 0 || rows[i - 1].section !== r.section ? r.section : null
            return (
              <Fragment key={r.key}>
                {header && (
                  <li className="cmdk__section" role="presentation">
                    {t(`cmdk.section.${header}`)}
                  </li>
                )}
                <li
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
                  <span className="cmdk__title">{highlight(r.title, needle)}</span>
                  {r.code && (
                    <span className="cmdk__code" aria-label="matched code">
                      <mark className="cmdk__hl">
                        {r.code.slice(0, r.matchedLen ?? 0)}
                      </mark>
                      {r.code.slice(r.matchedLen ?? 0)}
                    </span>
                  )}
                  {r.sub && <span className="cmdk__sub">{r.sub}</span>}
                </li>
              </Fragment>
            )
          })}
          {rows.length === 0 && (
            <li className="cmdk__empty">
              {q.trim() ? t('cmdk.noResults') : t('cmdk.hint')}
            </li>
          )}
        </ul>
      </div>
    </div>
  )
}
