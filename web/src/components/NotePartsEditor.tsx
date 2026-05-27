import { useCallback, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { authFetch, errMessage } from '../api/client'
import type { components } from '../api/schema'
import { GardenIcon } from './GardenIcon'
import { RichEditor } from './RichEditor'

type NotePart = components['schemas']['NotePartOut']

// Phase 4 of the multi-part rollout (task 29761355). Renders a note's
// ordered markdown parts as collapsible blocks with reorder, add,
// edit and delete. Sits BELOW the legacy transcript editor for now
// so both surfaces coexist; Phase 6 (task 1cd8bc0a) will remove the
// transcript editor entirely once every consumer has migrated.
//
// Reorder uses ↑/↓ buttons (one PUT /parts/order per swap) instead
// of HTML5 drag, which is fragile across browsers and would pull a
// heavier dnd library. The deferred-unique constraint on (note_id,
// ord) means a swap is a single server transaction; the SPA round-
// trips a fresh list after each move.

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

export function NotePartsEditor({ noteId }: { noteId: string }) {
  const { t } = useTranslation()
  const [parts, setParts] = useState<NotePart[]>([])
  const [err, setErr] = useState('')
  const [loading, setLoading] = useState(false)
  const [busyPid, setBusyPid] = useState<string | null>(null)
  const [editingBody, setEditingBody] = useState<Record<string, string>>({})

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

  const addPart = async () => {
    setErr('')
    const res = await authFetch(`/notes/${noteId}/parts`, {
      method: 'POST',
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
    if (!confirm(t('notes.parts.confirmDelete', { defaultValue: 'Remove this part?' }))) return
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

  const saveBody = async (part: NotePart) => {
    const draft = editingBody[part.id]
    if (draft === undefined || draft === (part.body ?? '')) return
    setBusyPid(part.id)
    try {
      const res = await authFetch(`/notes/${noteId}/parts/${part.id}`, {
        method: 'PATCH',
        body: JSON.stringify({ expected_version: part.version, body: draft }),
      })
      if (!res.ok) {
        setErr(errMessage(await res.json().catch(() => ({}))))
        return
      }
      // Drop the local draft and refresh from the server so version
      // bumps reflect (a stale PATCH would otherwise lose the user's
      // next edit to a concurrent writer).
      setEditingBody((cur) => {
        const out = { ...cur }
        delete out[part.id]
        return out
      })
      await reload()
    } finally {
      setBusyPid(null)
    }
  }

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
            defaultValue: 'No parts yet. Add one to start splitting the note into blocks.',
          })}
        </p>
      )}
      <ol className="parts-editor__list">
        {parts.map((p, i) => {
          const draft = editingBody[p.id] ?? p.body ?? ''
          const dirty = draft !== (p.body ?? '')
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
                  <div className="parts-editor__actions">
                    <button
                      type="button"
                      disabled={!dirty || busy}
                      onClick={() => void saveBody(p)}
                    >
                      {busy
                        ? t('notes.saving')
                        : t('notes.parts.save', { defaultValue: 'Save part' })}
                    </button>
                    {dirty && (
                      <button
                        type="button"
                        className="btn--ghost"
                        onClick={() =>
                          setEditingBody((cur) => {
                            const out = { ...cur }
                            delete out[p.id]
                            return out
                          })
                        }
                      >
                        {t('common.cancel')}
                      </button>
                    )}
                  </div>
                </div>
              )}
            </li>
          )
        })}
      </ol>
    </section>
  )
}
