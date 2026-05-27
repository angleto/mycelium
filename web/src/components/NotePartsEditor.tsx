import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
} from 'react'
import { useTranslation } from 'react-i18next'
import { authFetch, errMessage } from '../api/client'
import type { components } from '../api/schema'
import type { EditSession } from '../lib/useEditSession'
import { GardenIcon } from './GardenIcon'
import { RichEditor } from './RichEditor'

type NotePart = components['schemas']['NotePartOut']

// Phase 6 final body surface: each note part is an editable markdown
// block. There is no per-part Save button: the parent modal's Save
// button drives ``saveAllDirty`` via the imperative handle, and a
// debounced autosave keeps drafts on the server inside an open
// ``edit_session`` window so a sudden disconnect doesn't lose work.
//
// Reorder uses ↑/↓ (one PUT /parts/order per swap); the deferred-
// unique constraint on (note_id, ord) means a swap is a single
// server transaction. The SPA round-trips a fresh list after each
// move so versions stay current.

export interface NotePartsEditorHandle {
  /** Save every dirty part body in order. Returns true when every
   * PATCH succeeded (used by the modal Save button to chain into
   * the note PATCH only on a clean parts save). */
  saveAllDirty: () => Promise<boolean>
}

function firstNonEmptyLine(s: string): string {
  for (const line of (s || '').split('\n')) {
    const t = line.trim()
    if (t) return t
  }
  return ''
}

function truncate(s: string, n: number): string {
  return s.length <= n ? s : s.slice(0, n - 1) + '…'
}

interface Props {
  noteId: string
  editSession?: EditSession
  /** Called whenever the dirty-set of parts changes so the parent
   * modal can enable / disable its single Save button. */
  onDirtyChange?: (dirty: boolean) => void
}

export const NotePartsEditor = forwardRef<NotePartsEditorHandle, Props>(
  function NotePartsEditor({ noteId, editSession, onDirtyChange }, ref) {
    const { t } = useTranslation()
    const [parts, setParts] = useState<NotePart[]>([])
    const [err, setErr] = useState('')
    const [loading, setLoading] = useState(false)
    const [busyPid, setBusyPid] = useState<string | null>(null)
    const [editingBody, setEditingBody] = useState<Record<string, string>>({})
    // Per-part debounce timer for autosave. Keyed by part.id so each
    // part's keystrokes reset only its own timer.
    const autosaveTimers = useRef<Record<string, number>>({})
    // Latest parts list cached in a ref so the imperative
    // ``saveAllDirty`` from the parent always sees the freshest
    // versions even when called from a closure built earlier.
    const partsRef = useRef<NotePart[]>([])
    const editingBodyRef = useRef<Record<string, string>>({})
    useEffect(() => {
      partsRef.current = parts
    }, [parts])
    useEffect(() => {
      editingBodyRef.current = editingBody
    }, [editingBody])

    const reload = useCallback(async () => {
      setLoading(true)
      setErr('')
      try {
        const res = await authFetch(`/notes/${noteId}/parts`)
        if (!res.ok) {
          setErr(`HTTP ${res.status}`)
          return
        }
        setParts((await res.json()) as NotePart[])
      } finally {
        setLoading(false)
      }
    }, [noteId])

    useEffect(() => {
      void reload()
    }, [reload])

    // Lift the dirty signal up so the parent modal's Save button
    // can reflect part-level edits.
    useEffect(() => {
      if (!onDirtyChange) return
      const anyDirty = parts.some((p) => {
        const draft = editingBody[p.id]
        return draft !== undefined && draft !== (p.body ?? '')
      })
      onDirtyChange(anyDirty)
    }, [editingBody, parts, onDirtyChange])

    const addPart = async () => {
      setErr('')
      const res = await authFetch(`/notes/${noteId}/parts`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ body: '' }),
      })
      if (!res.ok) {
        setErr(errMessage(await res.json().catch(() => ({}))))
        return
      }
      await reload()
    }

    const toggleCollapsed = async (pid: string, current: boolean) => {
      setBusyPid(pid)
      try {
        const res = await authFetch(`/notes/${noteId}/parts/${pid}/ui-state`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ collapsed: !current }),
        })
        if (!res.ok) {
          setErr(errMessage(await res.json().catch(() => ({}))))
          return
        }
        const updated = (await res.json()) as NotePart
        setParts((cur) => cur.map((p) => (p.id === pid ? updated : p)))
      } finally {
        setBusyPid(null)
      }
    }

    const removePart = async (pid: string) => {
      if (!confirm(t('notes.parts.confirmDelete', { defaultValue: 'Remove this part?' })))
        return
      setBusyPid(pid)
      try {
        const res = await authFetch(`/notes/${noteId}/parts/${pid}`, {
          method: 'DELETE',
        })
        if (!res.ok && res.status !== 204) {
          setErr(errMessage(await res.json().catch(() => ({}))))
          return
        }
        await reload()
      } finally {
        setBusyPid(null)
      }
    }

    const move = async (pid: string, delta: -1 | 1) => {
      const idx = parts.findIndex((p) => p.id === pid)
      if (idx < 0) return
      const target = idx + delta
      if (target < 0 || target >= parts.length) return
      const next = parts.slice()
      ;[next[idx], next[target]] = [next[target], next[idx]]
      setBusyPid(pid)
      try {
        const res = await authFetch(`/notes/${noteId}/parts/order`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ part_ids: next.map((p) => p.id) }),
        })
        if (!res.ok) {
          setErr(errMessage(await res.json().catch(() => ({}))))
          return
        }
        setParts((await res.json()) as NotePart[])
      } finally {
        setBusyPid(null)
      }
    }

    const saveBody = useCallback(
      async (part: NotePart, draft: string): Promise<boolean> => {
        if (draft === (part.body ?? '')) return true
        setBusyPid(part.id)
        try {
          const headers: Record<string, string> = {
            'Content-Type': 'application/json',
          }
          if (editSession) headers['X-Edit-Session-Id'] = editSession.touch()
          const res = await authFetch(`/notes/${noteId}/parts/${part.id}`, {
            method: 'PATCH',
            headers,
            body: JSON.stringify({ expected_version: part.version, body: draft }),
          })
          if (!res.ok) {
            setErr(errMessage(await res.json().catch(() => ({}))))
            return false
          }
          setEditingBody((cur) => {
            const out = { ...cur }
            delete out[part.id]
            return out
          })
          return true
        } finally {
          setBusyPid(null)
        }
      },
      [editSession, noteId],
    )

    // Debounced autosave: 1.2s after the last keystroke on a part,
    // push the draft to the server. The note-level revision coalesces
    // these into one open row via ``X-Edit-Session-Id`` so the
    // recovery-history stays a single window per editing session.
    useEffect(() => {
      const timers = autosaveTimers.current
      for (const part of parts) {
        const draft = editingBody[part.id]
        if (draft === undefined || draft === (part.body ?? '')) continue
        if (timers[part.id]) window.clearTimeout(timers[part.id])
        timers[part.id] = window.setTimeout(() => {
          delete timers[part.id]
          void saveBody(part, draft).then((ok) => {
            if (ok) void reload()
          })
        }, 1200)
      }
      return () => {
        for (const pid of Object.keys(timers)) {
          if (editingBody[pid] === undefined) {
            window.clearTimeout(timers[pid])
            delete timers[pid]
          }
        }
      }
    }, [editingBody, parts, saveBody, reload])

    useImperativeHandle(
      ref,
      () => ({
        saveAllDirty: async () => {
          // Cancel any pending autosave timers; we're about to fire
          // the saves explicitly.
          const timers = autosaveTimers.current
          for (const pid of Object.keys(timers)) {
            window.clearTimeout(timers[pid])
            delete timers[pid]
          }
          let allOk = true
          for (const part of partsRef.current) {
            const draft = editingBodyRef.current[part.id]
            if (draft === undefined || draft === (part.body ?? '')) continue
            const ok = await saveBody(part, draft)
            if (!ok) {
              allOk = false
              break
            }
          }
          // Reload once at the end so the SPA sees fresh versions
          // for every saved part in a single round-trip.
          await reload()
          return allOk
        },
      }),
      [saveBody, reload],
    )

    return (
      <section className="parts-editor">
        <header className="parts-editor__head">
          <GardenIcon name="branch" size={14} />
          <strong>
            {t('notes.parts.title', { defaultValue: 'Parts' })} ({parts.length})
          </strong>
          <button
            type="button"
            className="btn--sm btn--ghost"
            onClick={() => void addPart()}
          >
            + {t('notes.parts.add', { defaultValue: 'Add part' })}
          </button>
        </header>
        {err && <p className="error">{err}</p>}
        {loading && <p className="hint">{t('common.loading')}</p>}
        {!loading && parts.length === 0 && (
          <p className="hint">
            {t('notes.parts.empty', {
              defaultValue:
                'No parts yet. Add one to start splitting the note into blocks.',
            })}
          </p>
        )}
        <ol className="parts-editor__list">
          {parts.map((p, i) => {
            const draft = editingBody[p.id] ?? p.body ?? ''
            const busy = busyPid === p.id
            return (
              <li key={p.id} className="parts-editor__item">
                <header className="parts-editor__item-head">
                  <button
                    type="button"
                    className="parts-editor__toggle"
                    onClick={() => void toggleCollapsed(p.id, !!p.ui_collapsed)}
                    aria-expanded={!p.ui_collapsed}
                    title={
                      p.ui_collapsed
                        ? t('notes.parts.expand', { defaultValue: 'Expand' })
                        : t('notes.parts.collapse', { defaultValue: 'Collapse' })
                    }
                  >
                    {p.ui_collapsed ? '▸' : '▾'}
                  </button>
                  <span className="parts-editor__ord muted">#{p.ord}</span>
                  {p.lang && (
                    <span className="chip chip--lang" title="lang">
                      {p.lang}
                    </span>
                  )}
                  {p.ui_collapsed && (
                    <span className="parts-editor__preview muted">
                      {truncate(firstNonEmptyLine(p.body ?? ''), 80)}
                    </span>
                  )}
                  <span className="parts-editor__spacer" />
                  <button
                    type="button"
                    className="btn--sm btn--ghost"
                    disabled={busy || i === 0}
                    onClick={() => void move(p.id, -1)}
                    title={t('notes.parts.moveUp', { defaultValue: 'Move up' })}
                  >
                    ↑
                  </button>
                  <button
                    type="button"
                    className="btn--sm btn--ghost"
                    disabled={busy || i === parts.length - 1}
                    onClick={() => void move(p.id, 1)}
                    title={t('notes.parts.moveDown', { defaultValue: 'Move down' })}
                  >
                    ↓
                  </button>
                  <button
                    type="button"
                    className="btn--sm btn--danger"
                    disabled={busy}
                    onClick={() => void removePart(p.id)}
                    title={t('notes.parts.removeHint', {
                      defaultValue: 'Remove this part',
                    })}
                  >
                    ×
                  </button>
                </header>
                {!p.ui_collapsed && (
                  <div className="parts-editor__body">
                    <RichEditor
                      value={draft}
                      onChange={(v) =>
                        setEditingBody((cur) => ({ ...cur, [p.id]: v }))
                      }
                      placeholder={t('notes.parts.placeholder', {
                        defaultValue: 'Markdown for this part…',
                      })}
                    />
                  </div>
                )}
              </li>
            )
          })}
        </ol>
      </section>
    )
  },
)
