import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
} from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { mentionLink } from '../lib/mentions'
import { api, errMessage, workspaceHeader } from '../api/client'
import { useSession } from '../auth/useSession'
import { RichEditor } from '../components/RichEditor'
import { MarkdownView } from '../components/Markdown'
import { NoteListItem } from '../components/NoteListItem'
import { TagPicker } from '../components/TagPicker'
import { TaskTimer } from '../components/TaskTimer'
import { useFocus } from '../lib/focus'
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
  const [searchParams, setSearchParams] = useSearchParams()
  const [notes, setNotes] = useState<Note[]>([])
  const [tags, setTags] = useState<Tag[]>([])
  const [fTag, setFTag] = useState('')
  const [cmd, setCmd] = useState('')
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [made, setMade] = useState<{ id: string; title: string } | null>(null)
  const [converting, setConverting] = useState<string | null>(null)
  const [convertedIds, setConvertedIds] = useState<Set<string>>(new Set())

  // Modal: a note open for edit (or a fresh draft being created).
  const [sel, setSel] = useState<Note | null>(null)
  const [eTitle, setETitle] = useState('')
  const [eText, setEText] = useState('')
  const [turns, setTurns] = useState<Turn[]>([])
  const [convMsg, setConvMsg] = useState('')
  const [noteSaving, setNoteSaving] = useState(false)
  const savedSnap = useRef<{ title: string; text: string }>({
    title: '',
    text: '',
  })
  // Create draft (modal in create mode, before the note exists).
  const [creating, setCreating] = useState(false)
  const [cKind, setCKind] = useState<Kind>('text')
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
      const [n, g] = await Promise.all([
        api.GET('/notes', {
          params: {
            header: workspaceHeader(),
            query: { ...(fTag ? { tag_id: fTag } : {}) },
          },
        }),
        api.GET('/tags', { params: { header: workspaceHeader() } }),
      ])
      if (!active) return
      if (n.data) setNotes(n.data)
      if (g.data) setTags(g.data)
    })()
    return () => {
      active = false
    }
  }, [activeId, fTag])

  // Focus (client/project) filters the list client-side, reactively.
  // A note belongs to a client and may have no project, so matching by
  // project alone hid client-only notes. In focus a note is shown when
  // its project is in scope, OR one of its tags is in scope, OR its
  // client tag is the focused client (client-only notes included).
  const shownNotes = focusActive
    ? notes.filter((n) => {
        if (n.project_id != null && focusIds.includes(n.project_id))
          return true
        const tagIds = (n.tags ?? []).map((g) => g.id)
        if (tagIds.some((id) => focusIds.includes(id))) return true
        if (
          focusClient &&
          (n.tags ?? []).some(
            (g) => g.kind === 'client' && g.id === focusClient,
          )
        )
          return true
        return false
      })
    : notes

  function closeModal() {
    setSel(null)
    setCreating(false)
  }

  // Esc closes the modal (the only implicit exit; the backdrop does
  // not, to avoid losing a long note by a stray click).
  useEffect(() => {
    if (!sel && !creating) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') closeModal()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [sel, creating])

  // Deep links (mention resolution): /notes?open=<id> opens the note
  // modal (view + edit, reusing it — no duplicate viewer); ?tag=<id>
  // pre-filters. Params are consumed then cleared.
  useEffect(() => {
    const openId = searchParams.get('open')
    const tagId = searchParams.get('tag')
    if (!openId && !tagId) return
    void (async () => {
      if (tagId) setFTag(tagId)
      if (openId) {
        const { data } = await api.GET('/notes/{note_id}', {
          params: { header: workspaceHeader(), path: { note_id: openId } },
        })
        if (data) await openEdit(data)
      }
      setSearchParams({}, { replace: true })
    })()
    // openEdit/setters are stable enough; params are cleared so this
    // runs once per incoming deep link.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams])

  async function openEdit(n: Note) {
    setErr(null)
    setMsg(null)
    setCreating(false)
    setSel(n)
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
    await loadNotes()
    // Continue in edit mode on the created note: tags / convert /
    // autosave are all available without leaving the modal.
    await openEdit(data)
  }

  const autoSaveNote = useCallback(async () => {
    if (!sel || sel.kind === 'conversation') return
    if (eTitle === savedSnap.current.title && eText === savedSnap.current.text)
      return
    setNoteSaving(true)
    const { data, error, response } = await api.PATCH('/notes/{note_id}', {
      params: { header: workspaceHeader(), path: { note_id: sel.id } },
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
  }, [sel, eTitle, eText, t, loadNotes])

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

  function inheritedTagIds(n: Note): string[] {
    return (n.tags ?? []).map((g) => g.id)
  }

  // Case 1 — the note IS the actionable: a task with the note's title +
  // body, inheriting its tags (the backend adds a default project if
  // none, so the task still resolves to a project/client), then the
  // note is archived (it has become the task).
  async function onConvert(n: Note) {
    if (converting !== null || convertedIds.has(n.id)) return
    setErr(null)
    setConverting(n.id)
    const label = n.title || n.kind
    const body = (n.transcript ?? '').trim()
    const { data, error } = await api.POST('/tasks', {
      params: { header: workspaceHeader() },
      body: {
        title: label,
        description:
          (body ? body + '\n\n' : '') +
          `From note: ${mentionLink('note', n.id, label)}`,
        priority: 3,
        executor_kind: 'human',
        necessity: 'should',
        tag_ids: inheritedTagIds(n),
      },
    })
    if (error || !data) {
      setConverting(null)
      setErr(errMessage(error))
      return
    }
    setConvertedIds((s) => new Set(s).add(n.id))
    setMade({ id: data.id, title: label })
    // The note has become the task: archive it (also closes the modal
    // when open, which disarms the autosave so it cannot race).
    await archiveNote(n)
    setConverting(null)
  }

  // Case 2 — a long/structured note: spin a task off the current text
  // selection (note stays, repeatable), inheriting the note's tags.
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
    const { data, error } = await api.POST('/tasks', {
      params: { header: workspaceHeader() },
      body: {
        title,
        description:
          selText +
          `\n\nFrom note: ${mentionLink('note', n.id, n.title || n.kind)}`,
        priority: 3,
        executor_kind: 'human',
        necessity: 'should',
        tag_ids: inheritedTagIds(n),
      },
    })
    setConverting(null)
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    setMade({ id: data.id, title })
  }

  const modalOpen = sel !== null || creating
  // Unsaved changes vs the last saved snapshot: the manual Save stays
  // available alongside autosave, enabled only while dirty.
  const noteDirty =
    !!sel &&
    sel.kind !== 'conversation' &&
    (eTitle !== savedSnap.current.title ||
      eText !== savedSnap.current.text)

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
        <select value={fTag} onChange={(e) => setFTag(e.target.value)}>
          <option value="">{t('notes.allTags')}</option>
          {tags.map((g) => (
            <option key={g.id} value={g.id}>
              {g.kind}: {g.name}
            </option>
          ))}
        </select>
      </div>

      <h2>{t('notes.yours')}</h2>
      {focusActive && (
        <p className="banner">
          {t('notes.focusOn', {
            shown: shownNotes.length,
            total: notes.length,
          })}
        </p>
      )}
      {shownNotes.length === 0 ? (
        <p className="hint">{t('notes.none')}</p>
      ) : (
        <ul className="list">
          {shownNotes.map((n) => (
            <NoteListItem
              key={n.id}
              note={n}
              converting={converting === n.id}
              converted={convertedIds.has(n.id)}
              onOpen={() => void openEdit(n)}
              onConvert={() => void onConvert(n)}
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
              <span className="modal__sp" />
              {!creating && sel && sel.kind !== 'conversation' && (
                <span className="muted">
                  {noteSaving ? t('notes.saving') : t('notes.autosaved')}
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
                {cKind !== 'conversation' && (
                  <label className="grow">
                    {t('notes.text')}
                    <RichEditor value={cText} onChange={setCText} large />
                  </label>
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
                {sel.task_id && (
                  <div className="notebanner">
                    <span>{t('notes.linkedTask')}</span>
                    <Link to={`/tasks/${sel.task_id}`}>
                      {t('notes.openTask')}
                    </Link>
                    <span className="modal__sp" />
                    <TaskTimer taskId={sel.task_id} />
                  </div>
                )}
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
                <label className="grow">
                  {t('notes.text')}
                  <RichEditor value={eText} onChange={setEText} large />
                </label>
              </div>
            )}
            {!creating && sel && sel.kind !== 'conversation' && (
              <div className="modal__foot">
                <button
                  type="button"
                  disabled={!noteDirty || noteSaving}
                  onClick={() => void autoSaveNote()}
                >
                  {noteSaving ? t('notes.saving') : t('notes.saveNote')}
                </button>
                <button
                  type="button"
                  className="btn--ghost"
                  title={t('notes.toTaskHint')}
                  disabled={converting !== null || convertedIds.has(sel.id)}
                  onClick={() => void onConvert(sel)}
                >
                  {convertedIds.has(sel.id)
                    ? t('notes.convertedShort')
                    : t('notes.toTask')}
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
                      <MarkdownView text={tr.content} />
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
