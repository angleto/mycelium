import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
} from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api, authFetch, errMessage, workspaceHeader } from '../api/client'
import { useSession } from '../auth/useSession'
import {
  NotePartsEditor,
  type NotePartsEditorHandle,
} from '../components/NotePartsEditor'
import { RichEditor } from '../components/RichEditor'
import { MarkdownView } from '../components/Markdown'
import { NoteListItem } from '../components/NoteListItem'
import { TagPicker } from '../components/TagPicker'
import { TagPickerGrid } from '../components/TagPickerGrid'
import { VoiceRecorder } from '../components/VoiceRecorder'
import { TaskTimer } from '../components/TaskTimer'
import { Attachments } from '../components/Attachments'
import { VoicePlayer } from '../components/VoicePlayer'
import { LinkedTasksPanel } from '../components/LinkedTasksPanel'
import { NoteLinksPanel } from '../components/NoteLinksPanel'
import { GardenSuggestionsPanel } from '../components/GardenSuggestionsPanel'
import { ChecklistPanel } from '../components/ChecklistPanel'
import { RevisionsPanel } from '../components/RevisionsPanel'
import { useFocus } from '../lib/focus'
import { useEditSession } from '../lib/useEditSession'
import type { components } from '../api/schema'

type Note = components['schemas']['NoteOut']
type Turn = components['schemas']['NoteTurnOut']
type Kind = components['schemas']['NoteKind']
type Tag = components['schemas']['TagOut']

const KINDS: Kind[] = ['text', 'voice', 'conversation']

// Notes: list with project + tag filters; create/edit happen in a
// modal you leave only on purpose (Esc or Close; the backdrop does not
// dismiss, and edits autosave so nothing is lost). Soft delete is
// reversible (Trash) so it does not confirm; erase is permanent and
// confirms hard.
export function NotesRoute() {
  const { t } = useTranslation()
  const session = useSession()
  const activeId = session?.workspaceId

  const {
    projectId: focusProject,
    clientId: focusClient,
    focusIds,
    active: focusActive,
  } = useFocus()
  const navigate = useNavigate()
  // ``/notes/:id`` opens the note modal on that id; ``/notes`` is the
  // bare list. The path is the canonical reference (UUID visible in the
  // address bar) — query ``?open=<id>`` is kept as legacy deep-link and
  // redirected here below.
  const { id: routeId } = useParams<{ id?: string }>()
  const [searchParams, setSearchParams] = useSearchParams()
  const [idCopied, setIdCopied] = useState(false)
  const [notes, setNotes] = useState<Note[]>([])
  const [loading, setLoading] = useState(true)
  const [tags, setTags] = useState<Tag[]>([])
  const [fTag, setFTag] = useState('')
  const [cmd, setCmd] = useState('')
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [made, setMade] = useState<{ id: string; title: string } | null>(null)
  const [converting, setConverting] = useState<string | null>(null)

  // Modal: a note open for edit (or a fresh draft being created).
  const [sel, setSel] = useState<Note | null>(null)
  const [linkTasks, setLinkTasks] = useState<
    { id: string; title: string }[]
  >([])
  const [eTitle, setETitle] = useState('')
  const [eText, setEText] = useState('')
  const [turns, setTurns] = useState<Turn[]>([])
  const [convMsg, setConvMsg] = useState('')
  const [noteSaving, setNoteSaving] = useState(false)
  // Mirror of NotePartsEditor's dirty flag, so the bottom Save
  // button can light up when the body changed even though the note
  // row itself hasn't.
  const [partsDirty, setPartsDirty] = useState(false)
  const partsEditorRef = useRef<NotePartsEditorHandle>(null)
  const savedSnap = useRef<{ title: string; text: string }>({
    title: '',
    text: '',
  })
  // Create draft (modal in create mode, before the note exists).
  const [creating, setCreating] = useState(false)
  const [cKind, setCKind] = useState<Kind>('text')
  // Voice-note recording buffer: the VoiceRecorder hands the parent
  // the captured Blob + mime + duration; doCreate uploads it AFTER
  // the note row is created (the upload endpoint wants a note_id).
  const [cAudioBlob, setCAudioBlob] = useState<Blob | null>(null)
  const [cAudioMime, setCAudioMime] = useState<string>('audio/webm')
  const [cAudioSeconds, setCAudioSeconds] = useState<number>(0)
  const [cTitle, setCTitle] = useState('')
  const [cText, setCText] = useState('')
  const [cProject, setCProject] = useState('')

  const projects = tags.filter((x) => x.kind === 'project')

  const loadNotes = useCallback(async () => {
    const { data } = await api.GET('/notes', {
      params: {
        header: workspaceHeader(),
        query: { ...(fTag ? { tag_id: fTag } : {}) },
      },
    })
    if (data) setNotes(data)
  }, [fTag])

  useEffect(() => {
    let active = true
    void (async () => {
      setLoading(true)
      try {
        const [n, g, tk] = await Promise.all([
          api.GET('/notes', {
            params: {
              header: workspaceHeader(),
              query: { ...(fTag ? { tag_id: fTag } : {}) },
            },
          }),
          api.GET('/tags', { params: { header: workspaceHeader() } }),
          api.GET('/tasks', { params: { header: workspaceHeader() } }),
        ])
        if (!active) return
        if (n.data) setNotes(n.data)
        if (g.data) setTags(g.data)
        if (tk.data)
          setLinkTasks(tk.data.map((x) => ({ id: x.id, title: x.title })))
      } finally {
        if (active) setLoading(false)
      }
    })()
    return () => {
      active = false
    }
  }, [activeId, fTag])

  // Focus (client/project) filters the list client-side, reactively.
  // A note belongs to a client and may have no project. In "client
  // focus" (no project narrowing) include the client-only notes via
  // the client tag; under project narrowing match only the project, so
  // narrowing a project does not bleed in client-only notes or notes
  // of sibling projects.
  const shownNotes = focusActive
    ? notes.filter((n) => {
        if (n.project_id != null && focusIds.includes(n.project_id))
          return true
        const tagIds = (n.tags ?? []).map((g) => g.id)
        if (tagIds.some((id) => focusIds.includes(id))) return true
        if (
          !focusProject &&
          focusClient &&
          (n.tags ?? []).some(
            (g) => g.kind === 'client' && g.id === focusClient,
          )
        )
          return true
        return false
      })
    : notes

  // closeModal now depends on routeId (it pops the URL only when
  // we're on /notes/:id), so memoise it — otherwise the Esc-listener
  // effect below would have to declare a fresh dep every render and
  // would either churn the listener or trip exhaustive-deps.
  const closeModal = useCallback(() => {
    setSel(null)
    setCreating(false)
    setIdCopied(false)
    // Keep the URL in sync with the modal: closing returns to /notes
    // so the path no longer points at a hidden note. ``replace`` so the
    // browser back button doesn't trap the user in a "go back, modal
    // reopens, close, modal reopens..." loop.
    if (routeId) navigate('/notes', { replace: true })
  }, [routeId, navigate])

  // Esc closes the modal (the only implicit exit; the backdrop does
  // not, to avoid losing a long note by a stray click).
  useEffect(() => {
    if (!sel && !creating) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') closeModal()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [sel, creating, closeModal])

  // Deep links: ``/notes?open=<id>`` is the legacy form (kept for
  // mentions stored in older note bodies and external bookmarks) —
  // it redirects to the canonical ``/notes/<id>`` so the URL settles
  // on a single shape. ``?tag=<id>`` pre-filters the list;
  // ``?action=new`` opens the create modal (PWA shortcut).
  useEffect(() => {
    const openId = searchParams.get('open')
    const tagId = searchParams.get('tag')
    const action = searchParams.get('action')
    if (!openId && !tagId && action !== 'new') return
    // Single-shot URL handoff: params get cleared at the end so this
    // effect re-fires only on the next deep link. ``setFTag`` is the
    // URL→state pull that the lint rule flags; the others (navigate /
    // setSearchParams) are outside-React APIs and don't trip it.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (tagId) setFTag(tagId)
    if (openId) {
      setSearchParams({}, { replace: true })
      navigate(`/notes/${openId}`, { replace: true })
      return
    }
    if (action === 'new') openCreate()
    setSearchParams({}, { replace: true })
    // openCreate/setters are stable; params are cleared so this
    // runs once per incoming deep link.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams])

  // Path-param entry: ``/notes/<id>`` (canonical) or an arrival on
  // /notes/<id> via redirect from ``?open=<id>``. Load the note and
  // open the modal. ``openEdit`` is idempotent on the URL (it skips
  // ``navigate`` when ``routeId`` already matches), so this does not
  // loop with the URL sync inside ``openEdit`` / ``closeModal``.
  useEffect(() => {
    if (!routeId) return
    if (sel?.id === routeId) return
    void (async () => {
      const { data, error } = await api.GET('/notes/{note_id}', {
        params: { header: workspaceHeader(), path: { note_id: routeId } },
      })
      if (error || !data) {
        // Stale or invalid id (deleted note, wrong workspace,
        // typo in a pasted link): drop back to the list so the
        // user sees something useful instead of a stuck route.
        setErr(errMessage(error))
        navigate('/notes', { replace: true })
        return
      }
      await openEdit(data)
    })()
    // Depend ONLY on routeId. With ``sel?.id`` in the deps the effect
    // re-fires during the close sequence: setSel(null) and navigate()
    // are not atomic, so a render in between sees sel?.id=undefined
    // and routeId still=oldId; the effect would then re-fetch and
    // reopen the modal, requiring a second Close click. The
    // ``sel?.id === routeId`` guard inside still short-circuits the
    // redundant GET when a list-click set sel before routeId caught
    // up.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [routeId])

  async function openEdit(n: Note) {
    setErr(null)
    setMsg(null)
    setCreating(false)
    setSel(n)
    setIdCopied(false)
    savedSnap.current = { title: n.title ?? '', text: n.transcript ?? '' }
    setETitle(n.title ?? '')
    setEText(n.transcript ?? '')
    if (n.kind === 'conversation') {
      const { data } = await api.GET('/notes/{note_id}/turns', {
        params: { header: workspaceHeader(), path: { note_id: n.id } },
      })
      setTurns(data ?? [])
    } else {
      setTurns([])
    }
    // Canonical URL for the open note: /notes/<id>. ``replace`` so
    // clicking between notes in the list does not pile up history
    // entries the user would then have to back-step through.
    if (routeId !== n.id) navigate(`/notes/${n.id}`, { replace: true })
  }

  function openCreate() {
    setErr(null)
    setMsg(null)
    setSel(null)
    setCKind('text')
    setCTitle('')
    setCText('')
    setCProject(focusProject)
    setCreating(true)
  }

  async function doCreate() {
    setErr(null)
    const { data, error } = await api.POST('/notes', {
      params: { header: workspaceHeader() },
      body: {
        kind: cKind,
        title: cTitle || null,
        text: cText || null,
        project_id: cProject || null,
      },
    })
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    // Voice notes: upload the recorded audio as a note attachment,
    // then PATCH note.audio_ref to point at it and trigger an STT
    // transcription run (fire-and-forget — if no STT provider is
    // configured the call surfaces an error in the note's
    // ``last_error`` but the audio is still playable).
    if (cKind === 'voice' && cAudioBlob) {
      try {
        const ext = cAudioMime.includes('mp4')
          ? 'm4a'
          : cAudioMime.includes('ogg')
            ? 'ogg'
            : 'webm'
        const form = new FormData()
        form.append('file', cAudioBlob, `voice-${data.id}.${ext}`)
        const upRes = await authFetch(`/notes/${data.id}/attachments`, {
          method: 'POST',
          body: form,
        })
        if (upRes.ok) {
          const att = (await upRes.json()) as { id: string; filename: string }
          // audio_ref schema: ``attachment:<id>``. The STT provider
          // resolves it through ``get_attachment_store().get(...)``
          // when transcribe runs; until that resolver is wired the
          // value is at least an unambiguous breadcrumb.
          await api.PATCH('/notes/{note_id}', {
            params: {
              header: workspaceHeader(),
              path: { note_id: data.id },
            },
            body: {
              expected_version: data.version,
              audio_ref: `attachment:${att.id}`,
              audio_seconds: cAudioSeconds,
            },
          })
          // Best-effort transcribe; ignore failure.
          await api.POST('/notes/{note_id}/transcribe', {
            params: {
              header: workspaceHeader(),
              path: { note_id: data.id },
            },
            body: { operation_id: `transcribe-${data.id}`, embed: true },
          }).catch(() => undefined)
        }
      } catch {
        /* upload failure leaves the note in 'captured' state without
           audio_ref. The user can re-record + upload later via the
           edit view. */
      }
      setCAudioBlob(null)
      setCAudioMime('audio/webm')
      setCAudioSeconds(0)
    }
    await loadNotes()
    // Continue in edit mode on the created note: tags / convert /
    // autosave are all available without leaving the modal.
    await openEdit(data)
  }

  // Recovery-history coalescing on the SPA: a per-note editing
  // session id rides every autosave PATCH as ``X-Edit-Session-Id``;
  // the server merges consecutive PATCHes that share it into one
  // open revision. On unmount / 30s idle the session is sealed and
  // the next edit mints a fresh id; ``editSession.seal`` also fires
  // POST /edit-session/seal so the timeline closes the window
  // immediately rather than waiting 60s for the worker safety net.
  const editSession = useEditSession((sealedId) => {
    if (!sel) return
    void api.POST('/notes/{note_id}/edit-session/seal', {
      params: { header: workspaceHeader(), path: { note_id: sel.id } },
      body: { edit_session_id: sealedId },
    })
  })

  const autoSaveNote = useCallback(async () => {
    if (!sel || sel.kind === 'conversation') return
    if (eTitle === savedSnap.current.title && eText === savedSnap.current.text)
      return
    setNoteSaving(true)
    const sessionId = editSession.touch()
    const { data, error, response } = await api.PATCH('/notes/{note_id}', {
      params: {
        header: { ...workspaceHeader(), 'X-Edit-Session-Id': sessionId },
        path: { note_id: sel.id },
      },
      body: { expected_version: sel.version, title: eTitle, text: eText },
    })
    setNoteSaving(false)
    if (response.status === 409) {
      setErr(t('tasks.conflict'))
      await loadNotes()
      return
    }
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    savedSnap.current = { title: eTitle, text: eText }
    setSel((p) => (p ? { ...p, version: data.version, title: eTitle } : p))
  }, [sel, eTitle, eText, t, loadNotes, editSession])

  // Debounced autosave (1.2s after the last keystroke).
  useEffect(() => {
    if (!sel || sel.kind === 'conversation') return
    if (eTitle === savedSnap.current.title && eText === savedSnap.current.text)
      return
    const h = setTimeout(() => void autoSaveNote(), 1200)
    return () => clearTimeout(h)
  }, [eTitle, eText, sel, autoSaveNote])


  async function refreshSel() {
    if (!sel) return
    const { data } = await api.GET('/notes/{note_id}', {
      params: { header: workspaceHeader(), path: { note_id: sel.id } },
    })
    if (data) setSel(data)
    await loadNotes()
  }

  // Link / unlink the task this note logs work against. task_id null
  // unlinks; the server preserves the link when the field is absent
  // (so plain title/text autosave never touches it).
  async function linkTask(taskId: string | null) {
    if (!sel) return
    setErr(null)
    const { error, response } = await api.PATCH('/notes/{note_id}', {
      params: { header: workspaceHeader(), path: { note_id: sel.id } },
      body: { expected_version: sel.version, task_id: taskId },
    })
    if (response.status === 409) {
      setErr(t('tasks.conflict'))
      await refreshSel()
      return
    }
    if (error) {
      setErr(errMessage(error))
      return
    }
    await refreshSel()
  }

  async function addTag(tagId: string) {
    if (!sel || !tagId) return
    setErr(null)
    const { error } = await api.POST('/notes/{note_id}/tags', {
      params: { header: workspaceHeader(), path: { note_id: sel.id } },
      body: { tag_id: tagId },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    await refreshSel()
  }

  async function removeTag(tagId: string) {
    if (!sel) return
    setErr(null)
    const { error } = await api.DELETE('/notes/{note_id}/tags/{tag_id}', {
      params: {
        header: workspaceHeader(),
        path: { note_id: sel.id, tag_id: tagId },
      },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    await refreshSel()
  }

  async function onCommand(e: FormEvent) {
    e.preventDefault()
    setErr(null)
    const { data, error } = await api.POST('/notes/command', {
      params: { header: workspaceHeader() },
      body: { text: cmd },
    })
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    await loadNotes()
    setCmd('')
  }

  // Closing the modal first disarms the debounced autosave (its effect
  // tears down on sel→null and clears the pending timer), so a stale
  // PATCH can't race a destructive op into a 409 "Stale version write".
  async function archiveNote(n: Note) {
    setErr(null)
    if (sel?.id === n.id) closeModal()
    const { error } = await api.POST('/notes/{note_id}/archive', {
      params: { header: workspaceHeader(), path: { note_id: n.id } },
      body: { expected_version: n.version },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    await loadNotes()
  }

  // Soft delete: reversible (Trash + Restore), so no confirmation.
  async function delNote(n: Note) {
    setErr(null)
    if (sel?.id === n.id) closeModal()
    const { error } = await api.POST('/notes/{note_id}/delete', {
      params: { header: workspaceHeader(), path: { note_id: n.id } },
      body: { expected_version: n.version },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    setMsg(t('notes.confirmDelete'))
    await loadNotes()
  }

  // Erase: permanent (note + memory), so it confirms hard.
  async function eraseNote(n: Note) {
    if (
      !window.confirm(
        t('notes.confirmErase', { title: n.title || n.kind }),
      )
    )
      return
    setErr(null)
    if (sel?.id === n.id) closeModal()
    const { error } = await api.POST('/notes/{note_id}/erase', {
      params: { header: workspaceHeader(), path: { note_id: n.id } },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    setNotes((xs) => xs.filter((x) => x.id !== n.id))
    setMsg(t('notes.erased'))
  }

  async function onSend(e: FormEvent) {
    e.preventDefault()
    if (!sel) return
    setErr(null)
    const { error } = await api.POST('/notes/{note_id}/messages', {
      params: { header: workspaceHeader(), path: { note_id: sel.id } },
      body: { content: convMsg, operation_id: crypto.randomUUID() },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    setConvMsg('')
    await openEdit(sel)
  }

  // Inherit the note's client tag (always present, added by
  // create_note) plus its project tag (also in the junction since
  // migration 0016) so the derived task lands under the same
  // project/client as its parent note. ``n.project_id`` is the
  // derived project-tag id; the guard against duplicates is
  // belt-and-braces since the project tag should already be in
  // ``n.tags``.
  function inheritedExtraTagIds(n: Note): string[] {
    const tagIds = (n.tags ?? []).map((g) => g.id)
    if (n.project_id && !tagIds.includes(n.project_id)) tagIds.push(n.project_id)
    return tagIds
  }

  // The note generates a task (ADR-0029 P1, kind=derived_from): a
  // typed note↔task link is created and the note stays alive, so the
  // same note can spawn many tasks ("fruit"). The task title defaults
  // to the note's title (or the first transcript line / kind as
  // fallback); the user can rename it on the task page. The note is
  // NOT archived: deriving is repeatable.
  async function onConvert(n: Note) {
    if (converting !== null) return
    setErr(null)
    setConverting(n.id)
    const label =
      n.title?.trim() ||
      (n.transcript ?? '').split('\n').find((l) => l.trim()) ||
      n.kind
    const title = label.slice(0, 290)
    const { data, error } = await api.POST(
      '/notes/{note_id}/derive-task',
      {
        params: { header: workspaceHeader(), path: { note_id: n.id } },
        body: {
          title,
          description: null,
          extra_tag_ids: inheritedExtraTagIds(n),
        },
      },
    )
    if (error || !data) {
      setConverting(null)
      setErr(errMessage(error))
      return
    }
    setMade({ id: data.task_id, title })
    // Refresh notes so primary_task_id_for_note (and any list-side
    // chips) reflect the new link.
    await loadNotes()
    setConverting(null)
    // Open the freshly-derived task in its own view so the user lands
    // directly on the new fruit instead of staying in the note modal
    // (task #892f40b1: "devono aprirsi le finestre").
    closeModal()
    navigate(`/tasks/${data.task_id}`)
  }

  // Transplant the note into a task (ADR-0029 P1, kind=promoted_from):
  // the note is marked ``promoted_at`` (service-layer read-only). This
  // is the 1:1 alternative to "Derive task" — pick it when the
  // thought IS the action, not when it spawns one.
  async function onPromote(n: Note) {
    if (n.promoted_at) return
    if (converting !== null) return
    if (!window.confirm(t('notes.promoteConfirm'))) return
    setErr(null)
    setConverting(n.id)
    const { data, error } = await api.POST(
      '/notes/{note_id}/promote',
      {
        params: { header: workspaceHeader(), path: { note_id: n.id } },
        body: { title: null },
      },
    )
    if (error || !data) {
      setConverting(null)
      setErr(errMessage(error))
      return
    }
    const title =
      n.title?.trim() ||
      (n.transcript ?? '').split('\n').find((l) => l.trim()) ||
      t('notes.untitled')
    setMade({ id: data.task_id, title })
    await loadNotes()
    setConverting(null)
    closeModal()
    navigate(`/tasks/${data.task_id}`)
  }

  // Case 2 — a long/structured note: spin a task off the current text
  // selection. Uses derive-task too (typed link, note stays alive,
  // repeatable) so this path is symmetric with the bare "Derive task"
  // action.
  async function onTaskFromSelection(n: Note) {
    if (converting !== null) return
    const selText = (window.getSelection()?.toString() ?? '').trim()
    if (!selText) {
      setErr(t('notes.selectFirst'))
      return
    }
    setErr(null)
    setConverting(n.id)
    const title = selText.split('\n')[0].slice(0, 80)
    const { data, error } = await api.POST(
      '/notes/{note_id}/derive-task',
      {
        params: { header: workspaceHeader(), path: { note_id: n.id } },
        body: {
          title,
          description: selText,
          extra_tag_ids: inheritedExtraTagIds(n),
        },
      },
    )
    setConverting(null)
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    setMade({ id: data.task_id, title })
    await loadNotes()
    closeModal()
    navigate(`/tasks/${data.task_id}`)
  }

  const modalOpen = sel !== null || creating
  // Unsaved changes vs the last saved snapshot: the manual Save stays
  // available alongside autosave, enabled only while dirty.
  const noteDirty =
    !!sel &&
    sel.kind !== 'conversation' &&
    (eTitle !== savedSnap.current.title ||
      eText !== savedSnap.current.text)
  // The bottom Save button covers BOTH the note row (title/text) and
  // every dirty part body. NotePartsEditor lifts its dirty flag here
  // so we can light the button up the moment the user edits a part.
  const anyDirty = noteDirty || partsDirty
  const saveAll = useCallback(async () => {
    // Save parts first; on a clean parts save we chain into the
    // note PATCH so a 409 on the note doesn't strand dirty parts.
    const partsOk = (await partsEditorRef.current?.saveAllDirty()) ?? true
    if (!partsOk) return
    if (noteDirty) await autoSaveNote()
  }, [noteDirty, autoSaveNote])

  return (
    <section className="card">
      <h1>{t('notes.title')}</h1>
      <p className="hint">{t('notes.meteredNote')}</p>
      {err && <p className="err">{err}</p>}
      {msg && <p className="ok">{msg}</p>}
      {made && (
        <p className="ok">
          {t('notes.converted')}:{' '}
          <Link to={`/tasks/${made.id}`}>{made.title}</Link>
        </p>
      )}

      <div className="row">
        <button type="button" onClick={openCreate}>
          {t('notes.newNote')}
        </button>
      </div>
      {tags.length > 0 && (
        <div className="row">
          <span className="muted">{t('notes.allTags')}:</span>
          <TagPickerGrid
            tags={tags}
            selected={fTag ? [fTag] : []}
            // Single-select on /notes: clicking the active chip clears
            // the filter, clicking a different one swaps it. Matches
            // the existing backend list call that takes one tag_id.
            onToggle={(id) => setFTag((cur) => (cur === id ? '' : id))}
            searchable={tags.length > 20}
          />
        </div>
      )}

      <h2>{t('notes.yours')}</h2>
      {focusActive && (
        <p className="banner">
          {t('notes.focusOn', {
            shown: shownNotes.length,
            total: notes.length,
          })}
        </p>
      )}
      {loading ? (
        <p className="hint" role="status" aria-live="polite">
          {t('common.loading')}
        </p>
      ) : shownNotes.length === 0 ? (
        <p className="hint">{t('notes.none')}</p>
      ) : (
        <ul className="list">
          {shownNotes.map((n) => (
            <NoteListItem
              key={n.id}
              note={n}
              converting={converting === n.id}
              derivedTaskTitles={(n.derived_task_ids ?? [])
                .map((id) => linkTasks.find((tk) => tk.id === id)?.title)
                .filter((s): s is string => Boolean(s))}
              onOpen={() => void openEdit(n)}
              onConvert={() => void onConvert(n)}
              onPromote={() => void onPromote(n)}
              onArchive={() => void archiveNote(n)}
              onDelete={() => void delNote(n)}
              onErase={() => void eraseNote(n)}
            />
          ))}
        </ul>
      )}

      <h2>{t('notes.cmdTitle')}</h2>
      <p className="hint">{t('notes.cmdHint')}</p>
      <form onSubmit={(e) => void onCommand(e)} className="row">
        <input
          required
          placeholder={t('notes.commandPh')}
          value={cmd}
          onChange={(e) => setCmd(e.target.value)}
        />
        <button type="submit">{t('notes.run')}</button>
      </form>

      {modalOpen && (
        <div
          className="modal__backdrop"
          role="dialog"
          aria-modal="true"
          aria-label={creating ? t('notes.newNote') : t('notes.editing')}
        >
          <div className="modal__panel">
            <div className="modal__head">
              <strong>
                {creating ? t('notes.newNote') : t('notes.editing')}
              </strong>
              {!creating && sel && (
                // Tiny clickable chip exposing the note id so the user
                // can paste it elsewhere (e.g. share a reference with an
                // assistant) without having to dig into the address bar.
                // The full UUID lives in ``title`` for hover + a11y; the
                // visible label keeps the head compact.
                <button
                  type="button"
                  className="chip"
                  title={idCopied ? t('notes.idCopied') : sel.id}
                  aria-label={t('notes.copyId')}
                  onClick={async () => {
                    try {
                      await navigator.clipboard.writeText(sel.id)
                      setIdCopied(true)
                      window.setTimeout(() => setIdCopied(false), 1500)
                    } catch {
                      // Clipboard unavailable (insecure context or
                      // permission denied): the id is still readable
                      // from the address bar / tooltip, so we just
                      // surface a transient hint via state.
                      setIdCopied(false)
                    }
                  }}
                >
                  {idCopied ? t('notes.idCopied') : `ID ${sel.id.slice(0, 8)}…`}
                </button>
              )}
              <span className="modal__sp" />
              {!creating && sel && sel.kind !== 'conversation' && (
                <span className="muted">
                  {noteSaving
                    ? t('notes.saving')
                    : anyDirty
                      ? t('notes.unsaved', { defaultValue: 'Unsaved' })
                      : t('notes.autosaved')}
                </span>
              )}
              <button
                type="button"
                className="btn--ghost btn--sm"
                onClick={closeModal}
              >
                {t('notes.close')}
              </button>
            </div>

            {creating && (
              <div className="modal__body">
                <div className="row">
                  <select
                    value={cKind}
                    onChange={(e) => setCKind(e.target.value as Kind)}
                  >
                    {KINDS.map((k) => (
                      <option key={k} value={k}>
                        {k}
                      </option>
                    ))}
                  </select>
                  <input
                    placeholder={t('notes.noteTitle')}
                    value={cTitle}
                    onChange={(e) => setCTitle(e.target.value)}
                  />
                  <select
                    value={cProject}
                    onChange={(e) => setCProject(e.target.value)}
                  >
                    <option value="">{t('notes.noProject')}</option>
                    {projects.map((p) => (
                      <option key={p.id} value={p.id}>
                        {p.name}
                      </option>
                    ))}
                  </select>
                </div>
                {cKind === 'voice' && (
                  <VoiceRecorder
                    onRecorded={(blob, mime, durSec) => {
                      setCAudioBlob(blob)
                      setCAudioMime(mime)
                      setCAudioSeconds(durSec)
                    }}
                  />
                )}
                {cKind !== 'conversation' && (
                  <div className="field grow">
                    {t('notes.text')}
                    <RichEditor
                      value={cText}
                      onChange={setCText}
                      large
                      filename={cTitle || 'note'}
                    />
                  </div>
                )}
              </div>
            )}
            {creating && (
              <div className="modal__foot">
                <button type="button" onClick={() => void doCreate()}>
                  {t('notes.create')}
                </button>
                <button
                  type="button"
                  className="btn--ghost"
                  onClick={closeModal}
                >
                  {t('notes.close')}
                </button>
              </div>
            )}

            {!creating && sel && sel.kind !== 'conversation' && (
              <div className="modal__body">
                <div className="notebanner">
                  {sel.task_id ? (
                    <>
                      <span>{t('notes.linkedTask')}</span>
                      {/* The task TITLE is the link: visible inline (no
                          longer hidden behind a generic "Open task"),
                          and clicking it still opens the task. Falls
                          back to the generic label for payloads that
                          predate ``task_title`` on NoteOut. */}
                      <Link
                        to={`/tasks/${sel.task_id}`}
                        className="notebanner__task"
                        title={t('notes.openTask')}
                      >
                        {sel.task_title ?? t('notes.openTask')}
                      </Link>
                      <button
                        type="button"
                        className="btn--sm btn--ghost"
                        title={t('notes.unlinkTaskHint')}
                        onClick={() => void linkTask(null)}
                      >
                        {t('notes.unlinkTask')}
                      </button>
                      <span className="modal__sp" />
                      {/* Start (⏱▶ serial / ⏱▶▶ parallel) and stop the
                          timer right here -- billed to the linked task
                          without opening it. ``labeled`` spells the
                          start actions out so the affordance is not
                          missed in the banner. */}
                      <TaskTimer
                        taskId={sel.task_id}
                        noteId={sel.id}
                        labeled
                      />
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
                <input
                  placeholder={t('notes.titlePlaceholder')}
                  value={eTitle}
                  onChange={(e) => setETitle(e.target.value)}
                />
                <TagPicker
                  selected={sel.tags ?? []}
                  all={tags}
                  onAdd={(tid) => void addTag(tid)}
                  onRemove={(tid) => void removeTag(tid)}
                />
                {/* Legacy single-body RichEditor removed: NotePartsEditor
                    is now the canonical body surface. ``eText`` state is
                    still derived from ``note.transcript`` (concatenated
                    parts) so the autosave/revision diff path keeps
                    working until those flows are fully migrated. */}
                {sel.kind === 'voice' && sel.audio_ref && (
                  <VoicePlayer
                    audioRef={sel.audio_ref}
                    audioSeconds={sel.audio_seconds}
                  />
                )}
                <NotePartsEditor
                  ref={partsEditorRef}
                  noteId={sel.id}
                  noteTitle={sel.title ?? eTitle ?? ''}
                  editSession={editSession}
                  onDirtyChange={setPartsDirty}
                />
                <Attachments noteId={sel.id} />
                <section className="note-checklist">
                  <h3 className="note-checklist__heading">
                    {t('notes.checklistHeading')}
                  </h3>
                  <ChecklistPanel
                    owner={{ kind: 'note', id: sel.id }}
                    disabled={sel.deleted_at != null || sel.is_archived}
                  />
                </section>
                <LinkedTasksPanel noteId={sel.id} />
                <NoteLinksPanel noteId={sel.id} />
                <GardenSuggestionsPanel
                  noteId={sel.id}
                  onApplied={() => void refreshSel()}
                />
                <RevisionsPanel
                  kind="note"
                  id={sel.id}
                  version={sel.version}
                  current={{
                    ...(sel as unknown as Record<string, unknown>),
                    // The SPA tracks the editable body separately while
                    // the user types; pass the live editor value so the
                    // diff vs the snapshot reflects what is on screen.
                    transcript: eText,
                    title: eTitle || sel.title,
                  }}
                  onRestored={() => void refreshSel()}
                />
              </div>
            )}
            {!creating && sel && sel.kind !== 'conversation' && (
              <div className="modal__foot">
                <button
                  type="button"
                  disabled={!anyDirty || noteSaving}
                  onClick={() => void saveAll()}
                >
                  {noteSaving ? t('notes.saving') : t('notes.saveNote')}
                </button>
                <button
                  type="button"
                  className="btn--ghost"
                  title={t('notes.toTaskHint')}
                  disabled={converting !== null}
                  onClick={() => void onConvert(sel)}
                >
                  {t('notes.toTask')}
                </button>
                <button
                  type="button"
                  className="btn--ghost"
                  title={t('notes.promoteHint')}
                  disabled={converting !== null || !!sel.promoted_at}
                  onClick={() => void onPromote(sel)}
                >
                  {sel.promoted_at
                    ? t('notes.promotedShort')
                    : t('notes.promote')}
                </button>
                <button
                  type="button"
                  className="btn--ghost"
                  disabled={converting !== null}
                  onClick={() => void onTaskFromSelection(sel)}
                >
                  {t('notes.fromSelection')}
                </button>
                <span className="modal__sp" />
                <button
                  type="button"
                  className="btn--ghost"
                  onClick={() => void archiveNote(sel)}
                >
                  {t('notes.archive')}
                </button>
                <button
                  type="button"
                  className="btn--ghost"
                  onClick={() => void delNote(sel)}
                >
                  {t('notes.deleteBtn')}
                </button>
                <button
                  type="button"
                  className="btn--danger"
                  onClick={() => void eraseNote(sel)}
                >
                  {t('notes.erase')}
                </button>
              </div>
            )}

            {!creating && sel && sel.kind === 'conversation' && (
              <div className="modal__body">
                <ul className="list">
                  {turns.map((tr) => (
                    <li key={tr.id}>
                      <strong>{tr.role}:</strong>{' '}
                      <MarkdownView
                        text={tr.content}
                        parent={{ kind: 'note', id: sel.id }}
                      />
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
              </div>
            )}
          </div>
        </div>
      )}
    </section>
  )
}
