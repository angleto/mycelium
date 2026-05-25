import { useCallback, useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import type { components } from '../api/schema'

type ChecklistItem = components['schemas']['TaskChecklistItemOut']

// Second tab next to the markdown description in the task view. Items
// are lightweight (text + done + position), never sub-tasks. The panel
// owns its own state and bubbles up the (done, total) count so the
// parent tab label can show "1/3" without re-fetching the task.
export function ChecklistPanel({
  taskId,
  initial,
  onCountChange,
  disabled = false,
}: {
  taskId: string
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
  // its identity: TaskDetailRoute passes an inline arrow (new reference
  // each render) and ``setChecklistCount({ done, total })`` builds a
  // fresh object every call (Object.is fails -> re-render is never
  // bailed out), so depending on the callback would loop forever
  // (open-task freeze with no console error, since this is not a
  // nested-during-render setState the runtime guards against).
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

  // The parent passes ``initial`` (embedded in TaskOut) so the panel
  // doesn't need a round-trip on first render. After that we own the
  // state autonomously: mutations go through the dedicated
  // /checklist endpoints and we never re-derive from a refreshed
  // parent payload. If the parent needs to force a hard reset (e.g.
  // after a 409 on the task itself), it should remount us with a
  // ``key={task.id + task.version}`` — cleaner than an effect that
  // races with our optimistic UI.

  const reload = useCallback(async (): Promise<void> => {
    const { data, error } = await api.GET('/tasks/{task_id}/checklist', {
      params: { header: workspaceHeader(), path: { task_id: taskId } },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    setItems(data ?? [])
  }, [taskId])

  const onAdd = useCallback(async (): Promise<void> => {
    const text = draft.trim()
    if (!text || disabled) return
    setBusy(true)
    setErr(null)
    const { data, error } = await api.POST('/tasks/{task_id}/checklist', {
      params: { header: workspaceHeader(), path: { task_id: taskId } },
      body: { text },
    })
    setBusy(false)
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    setItems((prev) => [...prev, data])
    setDraft('')
  }, [draft, disabled, taskId])

  const onToggle = useCallback(
    async (item: ChecklistItem): Promise<void> => {
      if (disabled) return
      setBusy(true)
      setErr(null)
      const { data, error } = await api.PATCH(
        '/tasks/{task_id}/checklist/{item_id}',
        {
          params: {
            header: workspaceHeader(),
            path: { task_id: taskId, item_id: item.id },
          },
          body: { expected_version: item.version, done: !item.done },
        },
      )
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
    [disabled, taskId, reload],
  )

  const onEditText = useCallback(
    async (item: ChecklistItem, nextText: string): Promise<void> => {
      const clean = nextText.trim()
      if (!clean || clean === item.text || disabled) return
      setBusy(true)
      setErr(null)
      const { data, error } = await api.PATCH(
        '/tasks/{task_id}/checklist/{item_id}',
        {
          params: {
            header: workspaceHeader(),
            path: { task_id: taskId, item_id: item.id },
          },
          body: { expected_version: item.version, text: clean },
        },
      )
      setBusy(false)
      if (error || !data) {
        setErr(errMessage(error))
        void reload()
        return
      }
      setItems((prev) => prev.map((it) => (it.id === data.id ? data : it)))
    },
    [disabled, taskId, reload],
  )

  const onRemove = useCallback(
    async (item: ChecklistItem): Promise<void> => {
      if (disabled) return
      setBusy(true)
      setErr(null)
      const { error } = await api.DELETE(
        '/tasks/{task_id}/checklist/{item_id}',
        {
          params: {
            header: workspaceHeader(),
            path: { task_id: taskId, item_id: item.id },
          },
        },
      )
      setBusy(false)
      if (error) {
        setErr(errMessage(error))
        void reload()
        return
      }
      setItems((prev) => prev.filter((it) => it.id !== item.id))
    },
    [disabled, taskId, reload],
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
      const { data, error } = await api.POST(
        '/tasks/{task_id}/checklist:reorder',
        {
          params: { header: workspaceHeader(), path: { task_id: taskId } },
          body: { ids: reordered.map((it) => it.id) },
        },
      )
      setBusy(false)
      if (error || !data) {
        setErr(errMessage(error))
        void reload()
        return
      }
      setItems(data)
    },
    [disabled, items, taskId, reload],
  )

  const onClearDone = useCallback(async (): Promise<void> => {
    if (disabled) return
    if (!window.confirm(t('tasks.checklistClearDoneConfirm'))) return
    setBusy(true)
    setErr(null)
    const { data, error } = await api.POST(
      '/tasks/{task_id}/checklist:clear_done',
      {
        params: { header: workspaceHeader(), path: { task_id: taskId } },
      },
    )
    setBusy(false)
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    if (data.removed > 0) {
      setItems((prev) => prev.filter((it) => !it.done))
    }
    window.alert(
      t('tasks.checklistClearDoneResult', { n: data.removed }),
    )
  }, [disabled, t, taskId])

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
  onRemove: () => void
  onMoveUp: () => void
  onMoveDown: () => void
}): React.ReactElement {
  const { t } = useTranslation()
  // Local draft for inline-edit so per-keystroke we don't fire a PATCH;
  // the commit happens on blur or Enter. We follow the React "adjust
  // state during render" pattern (https://react.dev/learn/you-might-not-need-an-effect):
  // when the canonical text changes from outside (server-side update,
  // PATCH from another tab) we resync the draft inline instead of
  // through an effect that would cascade an extra render.
  const [draft, setDraft] = useState(item.text)
  const [lastSeenText, setLastSeenText] = useState(item.text)
  if (item.text !== lastSeenText) {
    setLastSeenText(item.text)
    setDraft(item.text)
  }
  return (
    <li className={`checklist__row${item.done ? ' is-done' : ''}`}>
      <label className="checklist__check">
        <input
          type="checkbox"
          checked={item.done}
          disabled={disabled}
          onChange={onToggle}
        />
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
    </li>
  )
}
