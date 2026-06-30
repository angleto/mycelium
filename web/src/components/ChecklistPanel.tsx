import { useCallback, useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import type { components } from '../api/schema'
import { RichEditor } from './RichEditor'

type ChecklistItem = components['schemas']['TaskChecklistItemOut']
type ChecklistOwner = { kind: 'task' | 'note'; id: string }

// Lightweight ticked items inside a task OR a note (task bae178d2):
// one shared widget over the polymorphic owner. Items are not
// sub-tasks (text + done + position); each may carry an optional
// markdown ``body`` (the "articulate comment"), opened / edited as
// markdown inline. The panel owns its state and bubbles up the
// (done, total) count so the parent tab label can show "1/3".
export function ChecklistPanel({
  owner,
  initial,
  onCountChange,
  disabled = false,
}: {
  owner: ChecklistOwner
  initial?: ChecklistItem[]
  onCountChange?: (done: number, total: number) => void
  disabled?: boolean
}): React.ReactElement {
  const { t } = useTranslation()
  const [items, setItems] = useState<ChecklistItem[]>(initial ?? [])
  const [busy, setBusy] = useState(false)
  const [draft, setDraft] = useState('')
  const [err, setErr] = useState<string | null>(null)

  // Report counts to the parent (tab badge) whenever items change.
  // The callback is read through a ref so the effect never depends on
  // its identity (the parent passes a fresh inline arrow each render),
  // which would otherwise loop forever.
  const onCountChangeRef = useRef(onCountChange)
  useEffect(() => {
    onCountChangeRef.current = onCountChange
  }, [onCountChange])
  useEffect(() => {
    const cb = onCountChangeRef.current
    if (!cb) return
    const done = items.filter((it) => it.done).length
    cb(done, items.length)
  }, [items])

  // ---- owner-aware typed requests --------------------------------
  // openapi-fetch needs a literal path; the task and note checklist
  // sub-resources share the same request / response shape, so we
  // branch the path here and keep one code path for the handlers.
  // The task surface is byte-for-byte the previous behaviour.
  const ownerId = owner.id
  const ownerKind = owner.kind
  const listReq = useCallback(
    () =>
      ownerKind === 'task'
        ? api.GET('/tasks/{task_id}/checklist', {
            params: { header: workspaceHeader(), path: { task_id: ownerId } },
          })
        : api.GET('/notes/{note_id}/checklist', {
            params: { header: workspaceHeader(), path: { note_id: ownerId } },
          }),
    [ownerKind, ownerId],
  )
  const addReq = useCallback(
    (text: string) =>
      ownerKind === 'task'
        ? api.POST('/tasks/{task_id}/checklist', {
            params: { header: workspaceHeader(), path: { task_id: ownerId } },
            body: { text },
          })
        : api.POST('/notes/{note_id}/checklist', {
            params: { header: workspaceHeader(), path: { note_id: ownerId } },
            body: { text },
          }),
    [ownerKind, ownerId],
  )
  const patchReq = useCallback(
    (itemId: string, body: components['schemas']['TaskChecklistItemPatchIn']) =>
      ownerKind === 'task'
        ? api.PATCH('/tasks/{task_id}/checklist/{item_id}', {
            params: { header: workspaceHeader(), path: { task_id: ownerId, item_id: itemId } },
            body,
          })
        : api.PATCH('/notes/{note_id}/checklist/{item_id}', {
            params: { header: workspaceHeader(), path: { note_id: ownerId, item_id: itemId } },
            body,
          }),
    [ownerKind, ownerId],
  )
  const deleteReq = useCallback(
    (itemId: string) =>
      ownerKind === 'task'
        ? api.DELETE('/tasks/{task_id}/checklist/{item_id}', {
            params: { header: workspaceHeader(), path: { task_id: ownerId, item_id: itemId } },
          })
        : api.DELETE('/notes/{note_id}/checklist/{item_id}', {
            params: { header: workspaceHeader(), path: { note_id: ownerId, item_id: itemId } },
          }),
    [ownerKind, ownerId],
  )
  const reorderReq = useCallback(
    (ids: string[]) =>
      ownerKind === 'task'
        ? api.POST('/tasks/{task_id}/checklist:reorder', {
            params: { header: workspaceHeader(), path: { task_id: ownerId } },
            body: { ids },
          })
        : api.POST('/notes/{note_id}/checklist:reorder', {
            params: { header: workspaceHeader(), path: { note_id: ownerId } },
            body: { ids },
          }),
    [ownerKind, ownerId],
  )
  const clearDoneReq = useCallback(
    () =>
      ownerKind === 'task'
        ? api.POST('/tasks/{task_id}/checklist:clear_done', {
            params: { header: workspaceHeader(), path: { task_id: ownerId } },
          })
        : api.POST('/notes/{note_id}/checklist:clear_done', {
            params: { header: workspaceHeader(), path: { note_id: ownerId } },
          }),
    [ownerKind, ownerId],
  )

  const reload = useCallback(async (): Promise<void> => {
    const { data, error } = await listReq()
    if (error) {
      setErr(errMessage(error))
      return
    }
    setItems(data ?? [])
  }, [listReq])

  const onAdd = useCallback(async (): Promise<void> => {
    const text = draft.trim()
    if (!text || disabled) return
    setBusy(true)
    setErr(null)
    const { data, error } = await addReq(text)
    setBusy(false)
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    setItems((prev) => [...prev, data])
    setDraft('')
  }, [draft, disabled, addReq])

  const onToggle = useCallback(
    async (item: ChecklistItem): Promise<void> => {
      if (disabled) return
      setBusy(true)
      setErr(null)
      const { data, error } = await patchReq(item.id, {
        expected_version: item.version,
        done: !item.done,
      })
      setBusy(false)
      if (error || !data) {
        // On 409 (stale version) reload the canonical list so the user
        // sees the truth instead of the optimistic UI lying about state.
        setErr(errMessage(error))
        void reload()
        return
      }
      setItems((prev) => prev.map((it) => (it.id === data.id ? data : it)))
    },
    [disabled, patchReq, reload],
  )

  const onEditText = useCallback(
    async (item: ChecklistItem, nextText: string): Promise<void> => {
      const clean = nextText.trim()
      if (!clean || clean === item.text || disabled) return
      setBusy(true)
      setErr(null)
      const { data, error } = await patchReq(item.id, {
        expected_version: item.version,
        text: clean,
      })
      setBusy(false)
      if (error || !data) {
        setErr(errMessage(error))
        void reload()
        return
      }
      setItems((prev) => prev.map((it) => (it.id === data.id ? data : it)))
    },
    [disabled, patchReq, reload],
  )

  const onEditBody = useCallback(
    async (item: ChecklistItem, nextBody: string): Promise<void> => {
      if (disabled) return
      const normalised = nextBody.trim()
      if (normalised === (item.body ?? '').trim()) return
      setBusy(true)
      setErr(null)
      const { data, error } = await patchReq(item.id, {
        expected_version: item.version,
        body: normalised,
      })
      setBusy(false)
      if (error || !data) {
        setErr(errMessage(error))
        void reload()
        return
      }
      setItems((prev) => prev.map((it) => (it.id === data.id ? data : it)))
    },
    [disabled, patchReq, reload],
  )

  const onRemove = useCallback(
    async (item: ChecklistItem): Promise<void> => {
      if (disabled) return
      setBusy(true)
      setErr(null)
      const { error } = await deleteReq(item.id)
      setBusy(false)
      if (error) {
        setErr(errMessage(error))
        void reload()
        return
      }
      setItems((prev) => prev.filter((it) => it.id !== item.id))
    },
    [disabled, deleteReq, reload],
  )

  const onMove = useCallback(
    async (item: ChecklistItem, direction: -1 | 1): Promise<void> => {
      if (disabled) return
      const idx = items.findIndex((it) => it.id === item.id)
      const next = idx + direction
      if (idx < 0 || next < 0 || next >= items.length) return
      // Optimistic swap: rebuild the array, then issue a reorder
      // payload listing every current id in the new order.
      const reordered = items.slice()
      const tmp = reordered[idx]
      reordered[idx] = reordered[next]
      reordered[next] = tmp
      setItems(reordered)
      setBusy(true)
      setErr(null)
      const { data, error } = await reorderReq(reordered.map((it) => it.id))
      setBusy(false)
      if (error || !data) {
        setErr(errMessage(error))
        void reload()
        return
      }
      setItems(data)
    },
    [disabled, items, reorderReq, reload],
  )

  const onClearDone = useCallback(async (): Promise<void> => {
    if (disabled) return
    if (!window.confirm(t('tasks.checklistClearDoneConfirm'))) return
    setBusy(true)
    setErr(null)
    const { data, error } = await clearDoneReq()
    setBusy(false)
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    if (data.removed > 0) {
      setItems((prev) => prev.filter((it) => !it.done))
    }
    window.alert(t('tasks.checklistClearDoneResult', { n: data.removed }))
  }, [disabled, t, clearDoneReq])

  const doneCount = items.filter((it) => it.done).length
  const hasDone = doneCount > 0

  return (
    <div className="checklist">
      {err && <p className="error">{err}</p>}
      {items.length === 0 ? (
        <p className="muted">{t('tasks.checklistEmpty')}</p>
      ) : (
        <ul className="checklist__items">
          {items.map((it, idx) => (
            <ChecklistRow
              key={it.id}
              item={it}
              disabled={disabled || busy}
              canMoveUp={idx > 0}
              canMoveDown={idx < items.length - 1}
              onToggle={() => void onToggle(it)}
              onEditText={(next) => void onEditText(it, next)}
              onEditBody={(next) => void onEditBody(it, next)}
              onRemove={() => void onRemove(it)}
              onMoveUp={() => void onMove(it, -1)}
              onMoveDown={() => void onMove(it, 1)}
            />
          ))}
        </ul>
      )}
      <div className="checklist__add">
        <input
          type="text"
          value={draft}
          placeholder={t('tasks.checklistAddPlaceholder')}
          disabled={disabled || busy}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault()
              void onAdd()
            }
          }}
        />
        <button
          type="button"
          onClick={() => void onAdd()}
          disabled={disabled || busy || !draft.trim()}
        >
          {t('tasks.checklistAdd')}
        </button>
      </div>
      {hasDone && (
        <div className="checklist__footer">
          <button
            type="button"
            className="ghost"
            onClick={() => void onClearDone()}
            disabled={disabled || busy}
          >
            {t('tasks.checklistClearDone')}
          </button>
        </div>
      )}
    </div>
  )
}

function ChecklistRow({
  item,
  disabled,
  canMoveUp,
  canMoveDown,
  onToggle,
  onEditText,
  onEditBody,
  onRemove,
  onMoveUp,
  onMoveDown,
}: {
  item: ChecklistItem
  disabled: boolean
  canMoveUp: boolean
  canMoveDown: boolean
  onToggle: () => void
  onEditText: (next: string) => void
  onEditBody: (next: string) => void
  onRemove: () => void
  onMoveUp: () => void
  onMoveDown: () => void
}): React.ReactElement {
  const { t } = useTranslation()
  // Local draft for inline-edit so per-keystroke we don't fire a PATCH;
  // the commit happens on blur or Enter. Resync the draft inline when
  // the canonical text changes from outside (server update / other tab).
  const [draft, setDraft] = useState(item.text)
  const [lastSeenText, setLastSeenText] = useState(item.text)
  if (item.text !== lastSeenText) {
    setLastSeenText(item.text)
    setDraft(item.text)
  }
  // The articulate markdown comment is opt-in: an item with a body
  // starts expanded so it's not hidden; otherwise the leaf toggle
  // reveals the editor ("open as markdown"). Local draft committed on
  // explicit Save (the body editor is heavier than the one-line text).
  const hasBody = Boolean(item.body && item.body.trim())
  const [open, setOpen] = useState(hasBody)
  const [bodyDraft, setBodyDraft] = useState(item.body ?? '')
  const [lastSeenBody, setLastSeenBody] = useState(item.body ?? '')
  if ((item.body ?? '') !== lastSeenBody) {
    setLastSeenBody(item.body ?? '')
    setBodyDraft(item.body ?? '')
  }
  const bodyDirty = bodyDraft.trim() !== (item.body ?? '').trim()
  return (
    <li className={`checklist__row${item.done ? ' is-done' : ''}`}>
      <div className="checklist__row-main">
        <label className="checklist__check">
          <input type="checkbox" checked={item.done} disabled={disabled} onChange={onToggle} />
        </label>
        <input
          type="text"
          className="checklist__text"
          value={draft}
          disabled={disabled}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={() => onEditText(draft)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault()
              ;(e.target as HTMLInputElement).blur()
            }
            if (e.key === 'Escape') {
              setDraft(item.text)
              ;(e.target as HTMLInputElement).blur()
            }
          }}
        />
        <button
          type="button"
          className={`ghost icon${hasBody ? ' is-active' : ''}`}
          title={t('tasks.checklistNoteToggle')}
          aria-label={t('tasks.checklistNoteToggle')}
          aria-pressed={open}
          disabled={disabled}
          onClick={() => setOpen((v) => !v)}
        >
          {hasBody ? '📝' : '🗒'}
        </button>
        <button
          type="button"
          className="ghost icon"
          title={t('tasks.checklistMoveUp')}
          aria-label={t('tasks.checklistMoveUp')}
          disabled={disabled || !canMoveUp}
          onClick={onMoveUp}
        >
          ↑
        </button>
        <button
          type="button"
          className="ghost icon"
          title={t('tasks.checklistMoveDown')}
          aria-label={t('tasks.checklistMoveDown')}
          disabled={disabled || !canMoveDown}
          onClick={onMoveDown}
        >
          ↓
        </button>
        <button
          type="button"
          className="ghost icon"
          title={t('tasks.checklistRemove')}
          aria-label={t('tasks.checklistRemove')}
          disabled={disabled}
          onClick={onRemove}
        >
          ×
        </button>
      </div>
      {open && (
        <div className="checklist__note">
          <RichEditor
            value={bodyDraft}
            onChange={setBodyDraft}
            placeholder={t('tasks.checklistNotePlaceholder')}
            filename={item.text}
          />
          <div className="checklist__note-actions">
            <button
              type="button"
              className="btn--sm"
              disabled={disabled || !bodyDirty}
              onClick={() => onEditBody(bodyDraft)}
            >
              {t('tasks.checklistNoteSave')}
            </button>
            {bodyDirty && <span className="muted">{t('tasks.unsaved')}</span>}
          </div>
        </div>
      )}
    </li>
  )
}
