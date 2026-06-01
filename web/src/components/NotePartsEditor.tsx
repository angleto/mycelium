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
import { PartAnnotated } from './PartAnnotated'

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
  /** Surrounding note title; the parts editor forwards it (suffixed
   * with the part ord when there are multiple parts) as ``filename``
   * to each RichEditor so the Download MD / Export PDF actions
   * produce a recognisable file name. */
  noteTitle?: string
  editSession?: EditSession
  /** Called whenever the dirty-set of parts changes so the parent
   * modal can enable / disable its single Save button. */
  onDirtyChange?: (dirty: boolean) => void
}

export const NotePartsEditor = forwardRef<NotePartsEditorHandle, Props>(
  function NotePartsEditor(
    { noteId, noteTitle, editSession, onDirtyChange },
    ref,
  ) {
    const { t } = useTranslation()
    const [parts, setParts] = useState<NotePart[]>([])
    const [err, setErr] = useState('')
    const [loading, setLoading] = useState(false)
    const [busyPid, setBusyPid] = useState<string | null>(null)
    const [editingBody, setEditingBody] = useState<Record<string, string>>({})
    // Title draft keyed by part.id. Distinct from ``editingBody`` so
    // a user can edit just the title without bumping the body draft
    // (and viceversa). Empty string → clear the title (server stores
    // NULL); undefined here means "not edited" (use part.title).
    const [editingTitle, setEditingTitle] = useState<Record<string, string>>({})
    // Per-part debounce timer for autosave. Keyed by ``${pid}::body``
    // or ``${pid}::title`` so a title edit doesn't reset the body's
    // pending save (and viceversa).
    const autosaveTimers = useRef<Record<string, number>>({})
    // Latest parts list cached in a ref so the imperative
    // ``saveAllDirty`` from the parent always sees the freshest
    // versions even when called from a closure built earlier.
    const partsRef = useRef<NotePart[]>([])
    const editingBodyRef = useRef<Record<string, string>>({})
    const editingTitleRef = useRef<Record<string, string>>({})
    useEffect(() => {
      partsRef.current = parts
    }, [parts])
    useEffect(() => {
      editingBodyRef.current = editingBody
    }, [editingBody])
    useEffect(() => {
      editingTitleRef.current = editingTitle
    }, [editingTitle])

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
    // can reflect part-level edits (body OR title).
    useEffect(() => {
      if (!onDirtyChange) return
      const anyDirty = parts.some((p) => {
        const bDraft = editingBody[p.id]
        const tDraft = editingTitle[p.id]
        const bDirty = bDraft !== undefined && bDraft !== (p.body ?? '')
        const tDirty = tDraft !== undefined && tDraft !== (p.title ?? '')
        return bDirty || tDirty
      })
      onDirtyChange(anyDirty)
    }, [editingBody, editingTitle, parts, onDirtyChange])

    // ``ord``: when supplied, the new part lands at that index and
    // every part with ord ≥ value is shifted forward by one (the
    // backend handles the shift in a single deferred-unique tx). When
    // omitted the part goes to the tail (max(ord)+1). The "+ add part
    // below" button on each row passes the next ord so the new block
    // appears right after the current one without scrolling.
    const addPart = async (insertOrd?: number) => {
      setErr('')
      const payload: { body: string; ord?: number } = { body: '' }
      if (insertOrd !== undefined) payload.ord = insertOrd
      const res = await authFetch(`/notes/${noteId}/parts`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      if (!res.ok) {
        // Capture both the parsed-error message AND the raw status so
        // a server error that returns an empty / non-JSON body still
        // tells the user something useful (the previous version
        // surfaced "" silently on a 500 with no body).
        const parsed = await res.json().catch(() => ({}))
        const msg = errMessage(parsed)
        setErr(
          msg && msg !== 'Errore' && msg !== 'Error'
            ? msg
            : `HTTP ${res.status} ${res.statusText || ''}`.trim(),
        )
        // Surface the full response in the console so the user can
        // copy/paste it when the banner alone is not enough to
        // diagnose (used to triage the 63ebd516 silent-failure
        // report).
        console.error('[NotePartsEditor] addPart failed', {
          status: res.status,
          body: parsed,
        })
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

    // Single PATCH path: caller passes the patch payload (body and/or
    // title) and we route it through. Returns false on failure so the
    // chained autosaves can stop early. ``title: ''`` is canonicalised
    // to null so the server treats it as "clear the label".
    const patchPart = useCallback(
      async (
        part: NotePart,
        patch: { body?: string; title?: string | null },
      ): Promise<boolean> => {
        const hasBody = patch.body !== undefined
        const hasTitle = patch.title !== undefined
        if (!hasBody && !hasTitle) return true
        setBusyPid(part.id)
        try {
          const headers: Record<string, string> = {
            'Content-Type': 'application/json',
          }
          if (editSession) headers['X-Edit-Session-Id'] = editSession.touch()
          const payload: Record<string, unknown> = {
            expected_version: part.version,
          }
          if (hasBody) payload.body = patch.body
          if (hasTitle) payload.title = patch.title === '' ? null : patch.title
          const res = await authFetch(`/notes/${noteId}/parts/${part.id}`, {
            method: 'PATCH',
            headers,
            body: JSON.stringify(payload),
          })
          if (!res.ok) {
            setErr(errMessage(await res.json().catch(() => ({}))))
            return false
          }
          // The editor (and the title input) are the source of truth
          // while the user is typing. Take ONLY the authoritative
          // ``version`` from the server response and store the body /
          // title we actually SENT, not the server's re-serialised
          // echo. Feeding that echo back into ``parts[pid]`` and
          // clearing the local draft reverted the RichEditor ``value``
          // to a re-normalised string for a frame, which moved the
          // caret on every save and dropped any character typed while
          // the PATCH was in flight (the "cursor jumps / can't type"
          // bug). The drafts are intentionally KEPT: they are the live
          // mirror of the editor. They simply stop being "dirty"
          // because the stored body/title now equals what we sent (so
          // autosave does not re-fire on an idempotent round-trip),
          // while a keystroke that landed mid-flight leaves the draft
          // dirty and schedules the next autosave — no lost input.
          const updated = (await res.json()) as NotePart
          setParts((cur) =>
            cur.map((p) => {
              if (p.id !== part.id) return p
              const next: NotePart = { ...p, version: updated.version }
              if (hasBody) next.body = patch.body ?? ''
              if (hasTitle) next.title = patch.title === '' ? null : patch.title ?? null
              return next
            }),
          )
          return true
        } finally {
          setBusyPid(null)
        }
      },
      [editSession, noteId],
    )

    // Compatibility wrapper retained as the body-only entry point for
    // the autosave + saveAllDirty paths. ``saveTitle`` is its title
    // sibling; both delegate to ``patchPart``.
    const saveBody = useCallback(
      async (part: NotePart, draft: string): Promise<boolean> => {
        if (draft === (part.body ?? '')) return true
        return patchPart(part, { body: draft })
      },
      [patchPart],
    )

    const saveTitle = useCallback(
      async (part: NotePart, draft: string): Promise<boolean> => {
        if (draft === (part.title ?? '')) return true
        return patchPart(part, { title: draft })
      },
      [patchPart],
    )

    // Debounced autosave: 1.2s after the last keystroke on a part,
    // push the draft to the server. The note-level revision coalesces
    // these into one open row via ``X-Edit-Session-Id`` so the
    // recovery-history stays a single window per editing session.
    // Body and title have independent timer keys so an in-flight body
    // draft doesn't get cancelled by a title keystroke (and vice
    // versa).
    useEffect(() => {
      const timers = autosaveTimers.current
      for (const part of parts) {
        const bDraft = editingBody[part.id]
        if (bDraft !== undefined && bDraft !== (part.body ?? '')) {
          const k = `${part.id}::body`
          if (timers[k]) window.clearTimeout(timers[k])
          timers[k] = window.setTimeout(() => {
            delete timers[k]
            // No reload() on success: patchPart already syncs the
            // canonical ``version`` synchronously, and re-fetching the
            // server's re-normalised body here is exactly what used to
            // overwrite the live draft and jump the caret.
            void saveBody(part, bDraft)
          }, 1200)
        }
        const tDraft = editingTitle[part.id]
        if (tDraft !== undefined && tDraft !== (part.title ?? '')) {
          const k = `${part.id}::title`
          if (timers[k]) window.clearTimeout(timers[k])
          timers[k] = window.setTimeout(() => {
            delete timers[k]
            void saveTitle(part, tDraft)
          }, 1200)
        }
      }
      return () => {
        for (const key of Object.keys(timers)) {
          const [pid, field] = key.split('::') as [string, 'body' | 'title']
          const draft =
            field === 'body' ? editingBody[pid] : editingTitle[pid]
          if (draft === undefined) {
            window.clearTimeout(timers[key])
            delete timers[key]
          }
        }
      }
    }, [editingBody, editingTitle, parts, saveBody, saveTitle])

    useImperativeHandle(
      ref,
      () => ({
        saveAllDirty: async () => {
          // Cancel any pending autosave timers; we're about to fire
          // the saves explicitly.
          const timers = autosaveTimers.current
          for (const key of Object.keys(timers)) {
            window.clearTimeout(timers[key])
            delete timers[key]
          }
          let allOk = true
          for (const part of partsRef.current) {
            const bDraft = editingBodyRef.current[part.id]
            const tDraft = editingTitleRef.current[part.id]
            const bDirty =
              bDraft !== undefined && bDraft !== (part.body ?? '')
            const tDirty =
              tDraft !== undefined && tDraft !== (part.title ?? '')
            if (!bDirty && !tDirty) continue
            // Flush body + title together in one PATCH so the
            // server bumps ``version`` once per part, not twice.
            const ok = await patchPart(part, {
              ...(bDirty ? { body: bDraft } : {}),
              ...(tDirty ? { title: tDraft } : {}),
            })
            if (!ok) {
              allOk = false
              break
            }
          }
          // No reload() here: patchPart already synced each saved
          // part's ``version`` and stored what we sent, and the local
          // drafts are the live editor content. Re-fetching the
          // server's re-normalised bodies would only risk reverting
          // the caret or re-dirtying clean parts. The modal remounts
          // (fresh fetch) the next time the note is opened.
          return allOk
        },
      }),
      [patchPart],
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
            const titleDraft = editingTitle[p.id] ?? p.title ?? ''
            const busy = busyPid === p.id
            // Insert position for the per-row "Add below" action:
            // ``ord + 1`` lands the new part immediately after this
            // one and shifts the rest forward in a single deferred-
            // unique tx server-side. We pass the explicit value
            // instead of trusting ``i + 1`` because parts can have
            // sparse ords after a reorder.
            const insertAfterOrd = p.ord + 1
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
                      {truncate(
                        p.title || firstNonEmptyLine(p.body ?? ''),
                        80,
                      )}
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
                    className="btn--sm btn--ghost"
                    disabled={busy}
                    onClick={() => void addPart(insertAfterOrd)}
                    title={t('notes.parts.addBelowHint', {
                      defaultValue: 'Add a new part below this one',
                    })}
                  >
                    +{' '}
                    {t('notes.parts.addBelow', {
                      defaultValue: 'Add below',
                    })}
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
                    <input
                      type="text"
                      className="parts-editor__title"
                      value={titleDraft}
                      onChange={(e) =>
                        setEditingTitle((cur) => ({
                          ...cur,
                          [p.id]: e.target.value,
                        }))
                      }
                      placeholder={t('notes.parts.titlePlaceholder', {
                        defaultValue: 'Part title (optional)',
                      })}
                      maxLength={300}
                      aria-label={t('notes.parts.titleAria', {
                        defaultValue: 'Part title',
                      })}
                    />
                    <PartAnnotated
                      partId={p.id}
                      imageUploadParent={{ kind: 'note', id: noteId }}
                      value={draft}
                      onDocMutated={async () => {
                        // Accepting a suggestion splices the proposed
                        // text into this part's body server-side. Refetch
                        // the parts AND drop any local draft for this
                        // part, otherwise ``draft = editingBody[p.id] ??
                        // p.body`` would keep showing the pre-accept text.
                        await reload()
                        setEditingBody((cur) => {
                          if (!(p.id in cur)) return cur
                          const next = { ...cur }
                          delete next[p.id]
                          return next
                        })
                      }}
                      onChange={(v) =>
                        setEditingBody((cur) => ({ ...cur, [p.id]: v }))
                      }
                      placeholder={t('notes.parts.placeholder', {
                        defaultValue: 'Markdown for this part…',
                      })}
                      filename={
                        noteTitle
                          ? parts.length > 1
                            ? `${noteTitle} - part ${p.ord}`
                            : noteTitle
                          : `note-part-${p.ord}`
                      }
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
