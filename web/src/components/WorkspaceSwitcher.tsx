import { useEffect, useRef, useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'
import { api, errMessage } from '../api/client'
import { switchWorkspace } from '../auth/session'
import { useSession } from '../auth/useSession'
import { useMyWorkspace } from '../auth/useMyWorkspace'
import { reloadWorkspaces, useWorkspaces } from '../auth/useWorkspaces'
import { ARCHIVED, switchableWorkspaces } from '../lib/workspaceChoice'

// The active-workspace control, at the top of the sidebar.
//
// Switching is an in-app context switch, not a re-login (ADR-0024), so
// it belongs where the user can always reach it rather than three
// clicks deep in a settings page. Everything else about a workspace —
// renaming it, its members, archiving, deleting — lives in Settings →
// Workspace; this control does exactly two things: go somewhere, or
// make a new somewhere.
//
// Selecting a workspace calls `switchWorkspace`, which REMOUNTS the
// whole authenticated subtree (RequireAuth keys its Outlet on the
// workspace id). This component is inside that subtree, so it dies
// immediately after the call — deliberate: there is no post-switch
// state here worth preserving, and the remount is what guarantees no
// view keeps rendering the previous tenant's data.
export function WorkspaceSwitcher() {
  const { t } = useTranslation()
  const session = useSession()
  const activeId = session?.workspaceId ?? null
  const { list } = useWorkspaces()
  const { ws } = useMyWorkspace()
  const [open, setOpen] = useState(false)
  const [creating, setCreating] = useState(false)
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const wrapRef = useRef<HTMLDivElement | null>(null)
  const createRef = useRef<HTMLInputElement | null>(null)

  // House popover pattern (see MemoPopover): listeners armed only while
  // open, Escape stopped so it does not also close the mobile drawer.
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      e.stopPropagation()
      setOpen(false)
    }
    const onDown = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node))
        setOpen(false)
    }
    document.addEventListener('keydown', onKey)
    document.addEventListener('mousedown', onDown)
    return () => {
      document.removeEventListener('keydown', onKey)
      document.removeEventListener('mousedown', onDown)
    }
  }, [open])

  useEffect(() => {
    if (creating) createRef.current?.focus()
  }, [creating])

  function close() {
    setOpen(false)
    setCreating(false)
    setName('')
    setErr(null)
  }

  async function onCreate(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setErr(null)
    const { data, error } = await api.POST('/workspaces', {
      body: { name: name.trim() },
    })
    setBusy(false)
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    // Refresh the roster BEFORE switching: the switch remounts this
    // component, and a stale list would then be the first thing the new
    // context renders.
    await reloadWorkspaces()
    switchWorkspace(data.id)
  }

  // The name comes from the active-workspace fetch when it has landed
  // (authoritative, and it carries the archived flag), and from the
  // roster otherwise, so the control is never blank on a cold load.
  const fromList = list?.find((w) => w.id === activeId)
  const label = ws?.name ?? fromList?.name ?? '…'
  const isArchived = (ws?.status ?? fromList?.status) === ARCHIVED
  const options = switchableWorkspaces(list ?? [], activeId)

  return (
    <div className="wssw" ref={wrapRef}>
      <span className="focus__lbl">{t('wsmgr.label')}</span>
      <button
        type="button"
        className="wssw__trigger"
        aria-expanded={open}
        title={t('wsmgr.switchHint')}
        onClick={() => (open ? close() : setOpen(true))}
      >
        <span className="wssw__name">{label}</span>
        {isArchived && (
          <span className="tag tag--muted">{t('wsmgr.archived')}</span>
        )}
        <span className="wssw__caret" aria-hidden="true">
          ▾
        </span>
      </button>

      {/* No `role="menu"` below: the panel holds a text input and a link,
          which a menu may not contain, and the role would promise an
          arrow-key model this control does not implement. It is a
          disclosure (`aria-expanded` on the trigger) over plain buttons,
          which Tab already navigates correctly. */}
      {open && (
        <div className="wssw__menu">
          <ul className="wssw__list">
            {options.map((w) => (
              <li key={w.id}>
                <button
                  type="button"
                  className="wssw__row"
                  aria-current={w.id === activeId ? 'true' : undefined}
                  onClick={() => {
                    if (w.id === activeId) {
                      close()
                      return
                    }
                    switchWorkspace(w.id)
                  }}
                >
                  <span className="wssw__check" aria-hidden="true">
                    {w.id === activeId ? '✓' : ''}
                  </span>
                  <span className="wssw__rowname">{w.name}</span>
                  {w.status === ARCHIVED && (
                    <span className="tag tag--muted">{t('wsmgr.archived')}</span>
                  )}
                </button>
              </li>
            ))}
            {options.length === 0 && (
              <li className="wssw__hint">{t('wsmgr.loading')}</li>
            )}
          </ul>

          <div className="wssw__sep" />

          {creating ? (
            <form className="wssw__create" onSubmit={(e) => void onCreate(e)}>
              <input
                ref={createRef}
                required
                value={name}
                placeholder={t('wsmgr.newName')}
                onChange={(e) => setName(e.target.value)}
              />
              <button type="submit" className="btn--sm" disabled={busy || !name.trim()}>
                {busy ? t('wsmgr.creating') : t('wsmgr.create')}
              </button>
            </form>
          ) : (
            <button
              type="button"
              className="wssw__row"
              onClick={() => setCreating(true)}
            >
              <span className="wssw__check" aria-hidden="true">
                +
              </span>
              <span className="wssw__rowname">{t('wsmgr.create')}</span>
            </button>
          )}

          <Link
            to="/settings/workspace"
            className="wssw__row"
            onClick={close}
          >
            <span className="wssw__check" aria-hidden="true">
              ⚙
            </span>
            <span className="wssw__rowname">{t('wsmgr.manage')}</span>
          </Link>

          {err && <p className="err">{err}</p>}
        </div>
      )}
    </div>
  )
}
