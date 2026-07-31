import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
} from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api, authFetch, errMessage, workspaceHeader } from '../api/client'
import {
  NotePartsEditor,
  type NotePartsEditorHandle,
} from '../components/NotePartsEditor'
import { RefreshHint } from '../components/RefreshHint'
import { MarkdownView } from '../components/Markdown'
import { TagPicker } from '../components/TagPicker'
import { TaskTimer } from '../components/TaskTimer'
import { Attachments } from '../components/Attachments'
import { VoicePlayer } from '../components/VoicePlayer'
import { LinkedTasksPanel } from '../components/LinkedTasksPanel'
import { NoteLinksPanel } from '../components/NoteLinksPanel'
import { GardenSuggestionsPanel } from '../components/GardenSuggestionsPanel'
import { ChecklistPanel } from '../components/ChecklistPanel'
import { RevisionsPanel } from '../components/RevisionsPanel'
import { useEditSession } from '../lib/useEditSession'
import { useStaleWatch } from '../lib/useStaleWatch'
import { MOBILE_QUERY } from '../lib/useMediaQuery'
import type { components } from '../api/schema'

type Note = components['schemas']['NoteOut']
type Turn = components['schemas']['NoteTurnOut']
type Tag = components['schemas']['TagOut']
type Project = components['schemas']['ProjectOut']

// Note detail (full page, sibling of TaskDetailRoute). The note edit
// surface used to be a non-responsive 92vh modal stacking ~12 sections
// in one scroll column; ``/notes/:id`` is now a real page mirroring the
// task layout: a body-led main column, a collapsible "Details" aside
// (linked task + timer + tags) and a tabbed "Connections" region. The
// list route (/notes) keeps the modal only for quick-create.
//
// Optimistic concurrency: every write sends expected_version; a 409
// reloads the canonical note. Title autosaves (debounced) and the parts
// body autosaves per-part; the header status reflects the saved state.
export function NoteDetailRoute() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const { id = '' } = useParams<{ id: string }>()

  const [note, setNote] = useState<Note | null>(null)
  const [tags, setTags] = useState<Tag[]>([])
  // /projects, not the project tags: only ProjectOut carries
  // ``client_tag_id``, which couples the two structural selects.
  const [projects, setProjects] = useState<Project[]>([])
  const [linkTasks, setLinkTasks] = useState<{ id: string; title: string }[]>(
    [],
  )
  const [turns, setTurns] = useState<Turn[]>([])
  const [convMsg, setConvMsg] = useState('')
  const [err, setErr] = useState<string | null>(null)
  // Tag errors render inside the picker in the Details rail, not in the
  // page-level banner under the body: a rejected client change
  // (DomainError -> 400) has to be visible where it was triggered.
  const [tagErr, setTagErr] = useState<string | null>(null)
  const [converting, setConverting] = useState(false)
  const [idCopied, setIdCopied] = useState(false)

  const [eTitle, setETitle] = useState('')
  const [eText, setEText] = useState('')
  const [noteSaving, setNoteSaving] = useState(false)
  const [partsDirty, setPartsDirty] = useState(false)
  const partsEditorRef = useRef<NotePartsEditorHandle>(null)
  const knownPartsSig = useRef<string | null>(null)
  const savedSnap = useRef<{ title: string; text: string }>({
    title: '',
    text: '',
  })

  // The Details rail is collapsible on every viewport. Default: expanded
  // on desktop (a sticky side rail), collapsed on mobile (where it stacks
  // below the body, which leads). Collapsing it on desktop shrinks the
  // rail to a slim toggle so the note body reclaims the freed width. The
  // choice is remembered across notes — it's a layout preference, not a
  // per-note one.
  const [detailsOpen, setDetailsOpen] = useState<boolean>(() => {
    try {
      const v = localStorage.getItem('mycelium.note.detailsOpen')
      if (v === 'open') return true
      if (v === 'closed') return false
    } catch {
      /* private mode / quota */
    }
    try {
      return !window.matchMedia(MOBILE_QUERY).matches
    } catch {
      return true
    }
  })
  useEffect(() => {
    try {
      localStorage.setItem(
        'mycelium.note.detailsOpen',
        detailsOpen ? 'open' : 'closed',
      )
    } catch {
      /* ignore */
    }
  }, [detailsOpen])

  // Unified "Connections" group: linked tasks / note links / garden
  // suggestions share one tabbed surface. Active tab remembered per note.
  const connKey = `mycelium.note.${id}.connTab`
  const [connTab, setConnTab] = useState<'tasks' | 'ideas' | 'suggestions'>(
    () => {
      try {
        const v = localStorage.getItem(connKey)
        if (v === 'tasks' || v === 'ideas' || v === 'suggestions') return v
      } catch {
        /* private mode / quota */
      }
      return 'tasks'
    },
  )
  useEffect(() => {
    try {
      localStorage.setItem(connKey, connTab)
    } catch {
      /* ignore */
    }
  }, [connKey, connTab])

  // Load the note (+ tags and the task list that feeds the link-a-task
  // select). A stale / invalid id drops back to the list.
  useEffect(() => {
    let active = true
    void (async () => {
      const h = workspaceHeader()
      const [n, g, tk, pj] = await Promise.all([
        api.GET('/notes/{note_id}', {
          params: { header: h, path: { note_id: id } },
        }),
        api.GET('/tags', { params: { header: h } }),
        api.GET('/tasks', { params: { header: h } }),
        api.GET('/projects', { params: { header: h } }),
      ])
      if (!active) return
      if (n.error || !n.data) {
        setErr(errMessage(n.error))
        navigate('/notes', { replace: true })
        return
      }
      applyNote(n.data)
      if (g.data) setTags(g.data)
      if (pj.data) setProjects(pj.data)
      if (tk.data)
        setLinkTasks(tk.data.map((x) => ({ id: x.id, title: x.title })))
      if (n.data.kind === 'conversation') {
        const { data } = await api.GET('/notes/{note_id}/turns', {
          params: { header: h, path: { note_id: id } },
        })
        if (active) setTurns(data ?? [])
      }
    })()
    return () => {
      active = false
    }
    // applyNote is stable; navigate identity is stable.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id])

  function applyNote(n: Note) {
    setNote(n)
    savedSnap.current = { title: n.title ?? '', text: n.transcript ?? '' }
    setETitle(n.title ?? '')
    setEText(n.transcript ?? '')
  }

  async function refreshNote() {
    const { data } = await api.GET('/notes/{note_id}', {
      params: { header: workspaceHeader(), path: { note_id: id } },
    })
    if (data) setNote(data)
  }

  // Recovery-history coalescing: a per-note editing session id rides
  // every autosave PATCH as ``X-Edit-Session-Id`` so the server merges
  // consecutive PATCHes into one open revision; sealed on unmount / idle.
  const editSession = useEditSession((sealedId) => {
    void api.POST('/notes/{note_id}/edit-session/seal', {
      params: { header: workspaceHeader(), path: { note_id: id } },
      body: { edit_session_id: sealedId },
    })
  })

  const autoSaveNote = useCallback(async () => {
    if (!note || note.kind === 'conversation') return
    if (eTitle === savedSnap.current.title && eText === savedSnap.current.text)
      return
    setNoteSaving(true)
    const sessionId = editSession.touch()
    const { data, error, response } = await api.PATCH('/notes/{note_id}', {
      params: {
        header: { ...workspaceHeader(), 'X-Edit-Session-Id': sessionId },
        path: { note_id: id },
      },
      body: { expected_version: note.version, title: eTitle, text: eText },
    })
    setNoteSaving(false)
    if (response.status === 409) {
      setErr(t('tasks.conflict'))
      // Reload canonical truth so the next write starts from the fresh
      // version (inlined to keep this callback's deps minimal).
      const { data: fresh } = await api.GET('/notes/{note_id}', {
        params: { header: workspaceHeader(), path: { note_id: id } },
      })
      if (fresh) setNote(fresh)
      return
    }
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    savedSnap.current = { title: eTitle, text: eText }
    setNote((p) => (p ? { ...p, version: data.version, title: eTitle } : p))
  }, [note, eTitle, eText, id, t, editSession])

  // Debounced title autosave (1.2s after the last keystroke).
  useEffect(() => {
    if (!note || note.kind === 'conversation') return
    if (eTitle === savedSnap.current.title && eText === savedSnap.current.text)
      return
    const h = setTimeout(() => void autoSaveNote(), 1200)
    return () => clearTimeout(h)
  }, [eTitle, eText, note, autoSaveNote])

  // Out-of-band change detection (MCP / CLI / other device): the server
  // bumps ``version`` on every write but never pushes. On focus we
  // re-probe and raise a non-destructive banner instead of overwriting.
  useEffect(() => {
    knownPartsSig.current = null
  }, [id])
  const liftPartsSig = useCallback((s: string) => {
    knownPartsSig.current = s
  }, [])

  const noteStaleProbe = useCallback(async (): Promise<boolean> => {
    if (!note) return false
    const { data, error } = await api.GET('/notes/{note_id}', {
      params: { header: workspaceHeader(), path: { note_id: id } },
    })
    if (error || !data) return false
    if (data.version !== note.version) return true
    const res = await authFetch(`/notes/${id}/parts`)
    if (!res.ok) return false
    const parts = (await res.json()) as { id: string; version: number }[]
    const sig = parts.map((p) => `${p.id}:${p.version}`).join(',')
    return knownPartsSig.current !== null && sig !== knownPartsSig.current
  }, [note, id])

  const { stale: noteStale, reset: resetNoteStale } = useStaleWatch({
    enabled: !!note && note.kind !== 'conversation',
    resetKey: id,
    probe: noteStaleProbe,
  })

  async function reloadStaleNote() {
    const { data } = await api.GET('/notes/{note_id}', {
      params: { header: workspaceHeader(), path: { note_id: id } },
    })
    if (data) applyNote(data)
    await partsEditorRef.current?.discardAndReload()
    resetNoteStale()
  }

  async function linkTask(taskId: string | null) {
    if (!note) return
    setErr(null)
    const { error, response } = await api.PATCH('/notes/{note_id}', {
      params: { header: workspaceHeader(), path: { note_id: id } },
      body: { expected_version: note.version, task_id: taskId },
    })
    if (response.status === 409) {
      setErr(t('tasks.conflict'))
      await refreshNote()
      return
    }
    if (error) {
      setErr(errMessage(error))
      return
    }
    await refreshNote()
  }

  // Structural re-tagging through the named pair on NotePatchIn, not
  // attach/detach on /notes/{id}/tags: stating ``project_tag_id`` is
  // ONE intent (a MOVE -- the client follows and the memory blobs are
  // rescoped), and an explicit null is the un-share path (docs/adr/0021:
  // the note falls back to the client-level personal perimeter) instead
  // of the SPA having to find the attached project tag to delete it.
  // The patch carries no title/text, so the service's
  // replace-the-content path is not entered.
  async function setStructural(patch: {
    client_tag_id?: string
    project_tag_id?: string | null
  }) {
    if (!note) return
    setTagErr(null)
    const { error, response } = await api.PATCH('/notes/{note_id}', {
      params: { header: workspaceHeader(), path: { note_id: id } },
      body: { expected_version: note.version, ...patch },
    })
    if (response.status === 409) {
      setTagErr(t('tasks.conflict'))
      await refreshNote()
      return
    }
    if (error) {
      setTagErr(errMessage(error))
      return
    }
    await refreshNote()
  }

  // Free-form facets only: the pair goes through setStructural above.
  async function addTag(tagId: string) {
    if (!note || !tagId) return
    setTagErr(null)
    const { error } = await api.POST('/notes/{note_id}/tags', {
      params: { header: workspaceHeader(), path: { note_id: id } },
      body: { tag_id: tagId },
    })
    if (error) {
      setTagErr(errMessage(error))
      return
    }
    await refreshNote()
  }

  async function removeTag(tagId: string) {
    if (!note) return
    setTagErr(null)
    const { error } = await api.DELETE('/notes/{note_id}/tags/{tag_id}', {
      params: {
        header: workspaceHeader(),
        path: { note_id: id, tag_id: tagId },
      },
    })
    if (error) {
      setTagErr(errMessage(error))
      return
    }
    await refreshNote()
  }

  // Inherit the note's client + project tags so a derived task lands
  // under the same project/client as its parent note.
  function inheritedExtraTagIds(n: Note): string[] {
    const tagIds = (n.tags ?? []).map((g) => g.id)
    if (n.project_id && !tagIds.includes(n.project_id))
      tagIds.push(n.project_id)
    return tagIds
  }

  // Derive a task (kind=derived_from): the note stays alive and can
  // spawn many tasks. Land directly on the new task.
  async function onConvert() {
    if (!note || converting) return
    setErr(null)
    setConverting(true)
    const label =
      note.title?.trim() ||
      (note.transcript ?? '').split('\n').find((l) => l.trim()) ||
      note.kind
    const title = label.slice(0, 290)
    const { data, error } = await api.POST('/notes/{note_id}/derive-task', {
      params: { header: workspaceHeader(), path: { note_id: id } },
      body: { title, description: null, extra_tag_ids: inheritedExtraTagIds(note) },
    })
    if (error || !data) {
      setConverting(false)
      setErr(errMessage(error))
      return
    }
    navigate(`/tasks/${data.task_id}`)
  }

  // Promote (kind=promoted_from): the thought IS the action. The note is
  // marked read-only (service-layer); 1:1 alternative to Derive task.
  async function onPromote() {
    if (!note || note.promoted_at || converting) return
    if (!window.confirm(t('notes.promoteConfirm'))) return
    setErr(null)
    setConverting(true)
    const { data, error } = await api.POST('/notes/{note_id}/promote', {
      params: { header: workspaceHeader(), path: { note_id: id } },
      body: { title: null },
    })
    if (error || !data) {
      setConverting(false)
      setErr(errMessage(error))
      return
    }
    navigate(`/tasks/${data.task_id}`)
  }

  // Spin a task off the current text selection (derive-task, note stays
  // alive). Requires a selection inside the note body.
  async function onTaskFromSelection() {
    if (!note || converting) return
    const selText = (window.getSelection()?.toString() ?? '').trim()
    if (!selText) {
      setErr(t('notes.selectFirst'))
      return
    }
    setErr(null)
    setConverting(true)
    const title = selText.split('\n')[0].slice(0, 80)
    const { data, error } = await api.POST('/notes/{note_id}/derive-task', {
      params: { header: workspaceHeader(), path: { note_id: id } },
      body: {
        title,
        description: selText,
        extra_tag_ids: inheritedExtraTagIds(note),
      },
    })
    if (error || !data) {
      setConverting(false)
      setErr(errMessage(error))
      return
    }
    navigate(`/tasks/${data.task_id}`)
  }

  // Archive / soft-delete are reversible (no confirm); erase is
  // permanent (confirms hard). All return to the list afterwards.
  async function archiveNote() {
    if (!note) return
    setErr(null)
    const { error } = await api.POST('/notes/{note_id}/archive', {
      params: { header: workspaceHeader(), path: { note_id: id } },
      body: { expected_version: note.version },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    navigate('/notes')
  }

  // Fase P: ``protected`` marks finished prose the distiller never
  // compacts. Reversible toggle, stays on the page (unlike archive).
  async function protectNote() {
    if (!note) return
    setErr(null)
    const opts = {
      params: { header: workspaceHeader(), path: { note_id: id } },
      body: { expected_version: note.version },
    }
    const { error } = note.protected
      ? await api.POST('/notes/{note_id}/unprotect', opts)
      : await api.POST('/notes/{note_id}/protect', opts)
    if (error) {
      setErr(errMessage(error))
      return
    }
    await refreshNote()
  }

  // Fase P: on a humus atom, "ripristina originale" revives the hypha_of
  // source(s) and retires the atom (reversible: nothing is hard-deleted).
  async function restoreSource() {
    if (!note) return
    setErr(null)
    const { data, error } = await api.POST('/garden/review/restore-source', {
      params: { header: workspaceHeader() },
      body: { note_id: id },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    const target = data?.restored_source_ids?.[0] ?? data?.source_ids?.[0]
    navigate(target ? `/notes/${target}` : '/notes')
  }

  async function delNote() {
    if (!note) return
    setErr(null)
    const { error } = await api.POST('/notes/{note_id}/delete', {
      params: { header: workspaceHeader(), path: { note_id: id } },
      body: { expected_version: note.version },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    navigate('/notes')
  }

  async function eraseNote() {
    if (!note) return
    if (
      !window.confirm(
        t('notes.confirmErase', { title: note.title || note.kind }),
      )
    )
      return
    setErr(null)
    const { error } = await api.POST('/notes/{note_id}/erase', {
      params: { header: workspaceHeader(), path: { note_id: id } },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    navigate('/notes')
  }

  async function onSend(e: FormEvent) {
    e.preventDefault()
    if (!note) return
    setErr(null)
    const { error } = await api.POST('/notes/{note_id}/messages', {
      params: { header: workspaceHeader(), path: { note_id: id } },
      body: { content: convMsg, operation_id: crypto.randomUUID() },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    setConvMsg('')
    const { data } = await api.GET('/notes/{note_id}/turns', {
      params: { header: workspaceHeader(), path: { note_id: id } },
    })
    setTurns(data ?? [])
  }

  const noteDirty =
    !!note &&
    note.kind !== 'conversation' &&
    (eTitle !== savedSnap.current.title || eText !== savedSnap.current.text)
  const anyDirty = noteDirty || partsDirty

  // The Save button covers BOTH the note row (title) and every dirty
  // part body: save parts first, then chain into the note PATCH so a
  // 409 on the note doesn't strand dirty parts.
  const saveAll = useCallback(async () => {
    const partsOk = (await partsEditorRef.current?.saveAllDirty()) ?? true
    if (!partsOk) return
    if (noteDirty) await autoSaveNote()
  }, [noteDirty, autoSaveNote])

  async function copyId() {
    try {
      await navigator.clipboard.writeText(id)
      setIdCopied(true)
      window.setTimeout(() => setIdCopied(false), 1500)
    } catch {
      setIdCopied(false)
    }
  }

  if (err && !note) return <p className="err">{err}</p>
  if (!note) return <p className="hint">{t('common.loading')}</p>

  // Conversation notes are a chat surface: turn list + message box, no
  // body editor / tags / relationship panels.
  if (note.kind === 'conversation') {
    return (
      <section className="card card--wide notedetail">
        <header className="notedetail__header">
          <p className="hint notedetail__back">
            <Link to="/notes">{t('notes.back')}</Link>
          </p>
        </header>
        <h1 className="notedetail__title-h">
          {note.title || t('notes.untitled')}
        </h1>
        {err && <p className="err">{err}</p>}
        <ul className="list">
          {turns.map((tr) => (
            <li key={tr.id}>
              <strong>{tr.role}:</strong>{' '}
              <MarkdownView text={tr.content} parent={{ kind: 'note', id }} />
            </li>
          ))}
        </ul>
        <form onSubmit={(e) => void onSend(e)} className="row">
          <input
            required
            placeholder={t('notes.message')}
            value={convMsg}
            onChange={(e) => setConvMsg(e.target.value)}
          />
          <button type="submit">{t('notes.send')}</button>
        </form>
      </section>
    )
  }

  return (
    <section className="card card--wide notedetail">
      {noteStale && (
        <RefreshHint
          dirty={anyDirty}
          onReload={() => void reloadStaleNote()}
          onDismiss={resetNoteStale}
        />
      )}
      <header className="notedetail__header">
        <p className="hint notedetail__back">
          <Link to="/notes">{t('notes.back')}</Link>
        </p>
        <div className="notedetail__headeractions">
          <span
            className="notedetail__savestate hint"
            role="status"
            aria-live="polite"
          >
            {noteSaving
              ? t('notes.saving')
              : anyDirty
                ? t('notes.unsaved')
                : t('notes.autosaved')}
          </span>
          <button
            type="button"
            className="chip chip--copy"
            title={idCopied ? t('notes.idCopied') : id}
            aria-label={t('notes.copyId')}
            onClick={() => void copyId()}
          >
            {idCopied ? t('notes.idCopied') : `ID ${id.slice(0, 8)}…`}
          </button>
          <button
            type="button"
            className="btn--sm"
            disabled={anyDirty === false || noteSaving}
            onClick={() => void saveAll()}
          >
            {noteSaving ? t('notes.saving') : t('notes.saveNote')}
          </button>
          <span className="notedetail__headersep" aria-hidden="true" />
          <button
            type="button"
            className="btn--ghost btn--sm"
            title={t('notes.toTaskHint')}
            disabled={converting}
            onClick={() => void onConvert()}
          >
            {t('notes.toTask')}
          </button>
          <button
            type="button"
            className="btn--ghost btn--sm"
            title={t('notes.promoteHint')}
            disabled={converting || !!note.promoted_at}
            onClick={() => void onPromote()}
          >
            {note.promoted_at ? t('notes.promotedShort') : t('notes.promote')}
          </button>
          <button
            type="button"
            className="btn--ghost btn--sm"
            disabled={converting}
            onClick={() => void onTaskFromSelection()}
          >
            {t('notes.fromSelection')}
          </button>
          <button
            type="button"
            className="btn--ghost btn--sm"
            title={t('notes.protectHint')}
            onClick={() => void protectNote()}
          >
            {note.protected ? t('notes.unprotect') : t('notes.protect')}
          </button>
          {note.humus_kind != null && (
            <button
              type="button"
              className="btn--ghost btn--sm"
              title={t('notes.restoreSourceHint')}
              onClick={() => void restoreSource()}
            >
              {t('notes.restoreSource')}
            </button>
          )}
          <div className="notedetail__headeractions-danger">
            <button
              type="button"
              className="btn--ghost btn--sm"
              onClick={() => void archiveNote()}
            >
              {t('notes.archive')}
            </button>
            <button
              type="button"
              className="btn--danger btn--sm"
              onClick={() => void delNote()}
            >
              {t('notes.deleteBtn')}
            </button>
            <button
              type="button"
              className="btn--danger btn--sm"
              onClick={() => void eraseNote()}
            >
              {t('notes.erase')}
            </button>
          </div>
        </div>
      </header>

      {note.promoted_at && <p className="banner">{t('notes.promotedHint')}</p>}

      {/* Title spans full width above the two-pane grid (issue-tracker
          layout). onBlur flushes so a quick exit never loses it. */}
      <form
        className="notedetail__titleform"
        onSubmit={(e) => {
          e.preventDefault()
          void autoSaveNote()
        }}
      >
        <input
          className="notedetail__title"
          placeholder={t('notes.titlePlaceholder')}
          value={eTitle}
          onChange={(e) => setETitle(e.target.value)}
          onBlur={() => void autoSaveNote()}
          aria-label={t('notes.noteTitle')}
        />
      </form>

      <div
        className={
          'notedetail__grid' +
          (detailsOpen ? '' : ' notedetail__grid--asideclosed')
        }
      >
        <main className="notedetail__main">
          {note.kind === 'voice' && note.audio_ref && (
            <VoicePlayer
              audioRef={note.audio_ref}
              audioSeconds={note.audio_seconds}
            />
          )}
          <NotePartsEditor
            ref={partsEditorRef}
            noteId={id}
            noteTitle={note.title ?? eTitle ?? ''}
            editSession={editSession}
            onDirtyChange={setPartsDirty}
            onPartsSig={liftPartsSig}
          />
          <Attachments noteId={id} />
          <section className="note-checklist">
            <h3 className="note-checklist__heading">
              {t('notes.checklistHeading')}
            </h3>
            <ChecklistPanel
              owner={{ kind: 'note', id }}
              disabled={note.deleted_at != null || note.is_archived}
            />
          </section>
          {err && <p className="err">{err}</p>}
        </main>

        <aside
          className={
            'notedetail__aside' +
            (detailsOpen ? '' : ' notedetail__aside--collapsed')
          }
        >
          <button
            type="button"
            className="notedetail__propstoggle"
            aria-expanded={detailsOpen}
            title={t('notes.details')}
            onClick={() => setDetailsOpen((v) => !v)}
          >
            <span className="notedetail__propstoggle-label">
              {t('notes.details')}
            </span>
            <span aria-hidden="true" className="notedetail__propstoggle-caret">
              {detailsOpen ? '▾' : '▸'}
            </span>
          </button>
          {detailsOpen && (
            <div className="notedetail__asidebody">
              <div className="notebanner">
                {note.task_id ? (
                  <>
                    <span>{t('notes.linkedTask')}</span>
                    <Link
                      to={`/tasks/${note.task_id}`}
                      className="notebanner__task"
                      title={t('notes.openTask')}
                    >
                      {note.task_title ?? t('notes.openTask')}
                    </Link>
                    <button
                      type="button"
                      className="btn--sm btn--ghost"
                      title={t('notes.unlinkTaskHint')}
                      onClick={() => void linkTask(null)}
                    >
                      {t('notes.unlinkTask')}
                    </button>
                    <TaskTimer taskId={note.task_id} noteId={id} labeled />
                  </>
                ) : (
                  <>
                    <span>{t('notes.linkTaskPrompt')}</span>
                    <select
                      value=""
                      onChange={(e) =>
                        e.target.value && void linkTask(e.target.value)
                      }
                    >
                      <option value="">{t('notes.linkTaskPick')}</option>
                      {linkTasks.map((tk) => (
                        <option key={tk.id} value={tk.id}>
                          {tk.title}
                        </option>
                      ))}
                    </select>
                  </>
                )}
              </div>
              <h3 className="notedetail__asideh">{t('notes.tags')}</h3>
              <TagPicker
                selected={note.tags ?? []}
                all={tags}
                error={tagErr}
                structural={{
                  mode: 'note',
                  projects,
                  onSetClient: (cid) => {
                    if (cid) void setStructural({ client_tag_id: cid })
                  },
                  // ``null`` = "No project": un-share, keep the client.
                  onSetProject: (pid) =>
                    void setStructural({ project_tag_id: pid }),
                }}
                onAdd={(tid) => void addTag(tid)}
                onRemove={(tid) => void removeTag(tid)}
              />
            </div>
          )}
        </aside>
      </div>

      <section className="notedetail__connections">
        <h2 className="notedetail__connh">{t('notes.connections')}</h2>
        <div className="tabs" role="tablist" aria-label={t('notes.connections')}>
          <button
            type="button"
            role="tab"
            aria-selected={connTab === 'tasks'}
            className={`tabs__tab${connTab === 'tasks' ? ' is-active' : ''}`}
            onClick={() => setConnTab('tasks')}
          >
            {t('notes.tabTasks')}
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={connTab === 'ideas'}
            className={`tabs__tab${connTab === 'ideas' ? ' is-active' : ''}`}
            onClick={() => setConnTab('ideas')}
          >
            {t('notes.tabIdeas')}
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={connTab === 'suggestions'}
            className={`tabs__tab${connTab === 'suggestions' ? ' is-active' : ''}`}
            onClick={() => setConnTab('suggestions')}
          >
            {t('notes.tabSuggestions')}
          </button>
        </div>
        <div role="tabpanel" hidden={connTab !== 'tasks'}>
          <LinkedTasksPanel noteId={id} />
        </div>
        <div role="tabpanel" hidden={connTab !== 'ideas'}>
          <NoteLinksPanel noteId={id} />
        </div>
        <div role="tabpanel" hidden={connTab !== 'suggestions'}>
          <GardenSuggestionsPanel
            nodeId={id}
            nodeKind="note"
            onApplied={() => void refreshNote()}
          />
        </div>
      </section>

      <RevisionsPanel
        kind="note"
        id={id}
        version={note.version}
        current={{
          ...(note as unknown as Record<string, unknown>),
          transcript: eText,
          title: eTitle || note.title,
        }}
        onRestored={() => void refreshNote()}
      />
    </section>
  )
}
