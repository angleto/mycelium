import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
} from 'react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { mentionLink } from '../lib/mentions'
import { api, errMessage, workspaceHeader } from '../api/client'
import { useSession } from '../auth/useSession'
import { RichEditor } from '../components/RichEditor'
import { MarkdownView } from '../components/Markdown'
import { TagChip } from '../components/TagChip'
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

  const { projectId: focusProject } = useFocus()
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
        query: {
          ...(focusProject ? { project_id: focusProject } : {}),
          ...(fTag ? { tag_id: fTag } : {}),
        },
      },
    })
    if (data) setNotes(data)
  }, [focusProject, fTag])

  useEffect(() => {
    let active = true
    void (async () => {
      const [n, g] = await Promise.all([
        api.GET('/notes', {
          params: {
            header: workspaceHeader(),
            query: {
              ...(focusProject ? { project_id: focusProject } : {}),
              ...(fTag ? { tag_id: fTag } : {}),
            },
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
  }, [activeId, focusProject, fTag])

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

  async function archiveNote(n: Note) {
    setErr(null)
    const { error } = await api.POST('/notes/{note_id}/archive', {
      params: { header: workspaceHeader(), path: { note_id: n.id } },
      body: { expected_version: n.version },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    if (sel?.id === n.id) closeModal()
    await loadNotes()
  }

  // Soft delete: reversible (Trash + Restore), so no confirmation.
  async function delNote(n: Note) {
    setErr(null)
    const { error } = await api.POST('/notes/{note_id}/delete', {
      params: { header: workspaceHeader(), path: { note_id: n.id } },
      body: { expected_version: n.version },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    if (sel?.id === n.id) closeModal()
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
    const { error } = await api.POST('/notes/{note_id}/erase', {
      params: { header: workspaceHeader(), path: { note_id: n.id } },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    setNotes((xs) => xs.filter((x) => x.id !== n.id))
    if (sel?.id === n.id) closeModal()
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

  async function onConvert(n: Note) {
    if (converting !== null || convertedIds.has(n.id)) return
    setErr(null)
    setConverting(n.id)
    const label = n.title || n.kind
    const { data, error } = await api.POST('/tasks', {
      params: { header: workspaceHeader() },
      body: {
        title: label,
        description: `From note: ${mentionLink('note', n.id, label)}`,
        priority: 3,
        executor_kind: 'human',
        necessity: 'should',
      },
    })
    setConverting(null)
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    setConvertedIds((s) => new Set(s).add(n.id))
    setMade({ id: data.id, title: label })
  }

  const modalOpen = sel !== null || creating
  // Tags not already on the open note (for the add-tag picker).
  const addable = sel
    ? tags.filter((g) => !(sel.tags ?? []).some((s) => s.id === g.id))
    : []

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
      {notes.length === 0 ? (
        <p className="hint">{t('notes.none')}</p>
      ) : (
        <ul className="list">
          {notes.map((n) => (
            <li key={n.id}>
              <button
                type="button"
                className="btn--ghost btn--sm"
                onClick={() => void openEdit(n)}
              >
                {t('notes.open')}
              </button>{' '}
              {n.title || n.kind}{' '}
              <span className="muted">
                · {n.kind} · {n.status}
              </span>{' '}
              {(n.tags ?? []).map((g) => (
                <TagChip key={g.id} name={g.name} color={g.color} kind={g.kind} />
              ))}
              <button
                type="button"
                className="btn--sm"
                disabled={converting !== null || convertedIds.has(n.id)}
                onClick={() => void onConvert(n)}
              >
                {converting === n.id
                  ? t('notes.converting')
                  : convertedIds.has(n.id)
                    ? t('notes.convertedShort')
                    : t('notes.toTask')}
              </button>
              <button
                type="button"
                className="btn--ghost btn--sm"
                onClick={() => void archiveNote(n)}
              >
                {t('notes.archive')}
              </button>
              <button
                type="button"
                className="btn--ghost btn--sm"
                onClick={() => void delNote(n)}
              >
                {t('notes.deleteBtn')}
              </button>
              <button
                type="button"
                className="btn--danger btn--sm"
                title={t('notes.eraseHint')}
                onClick={() => void eraseNote(n)}
              >
                {t('notes.erase')}
              </button>
            </li>
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
                  <label>
                    {t('notes.text')}
                    <RichEditor value={cText} onChange={setCText} large />
                  </label>
                )}
                <div className="row">
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
              </div>
            )}

            {!creating && sel && sel.kind !== 'conversation' && (
              <div className="modal__body">
                <input
                  placeholder={t('notes.titlePlaceholder')}
                  value={eTitle}
                  onChange={(e) => setETitle(e.target.value)}
                />
                <div className="chips">
                  {(sel.tags ?? []).map((g) => (
                    <button
                      key={g.id}
                      type="button"
                      className="chip chip--rm"
                      title={t('notes.tags')}
                      onClick={() => void removeTag(g.id)}
                    >
                      {g.name} ✕
                    </button>
                  ))}
                  <select
                    value=""
                    onChange={(e) => void addTag(e.target.value)}
                  >
                    <option value="">{t('notes.addTag')}</option>
                    {addable.map((g) => (
                      <option key={g.id} value={g.id}>
                        {g.kind}: {g.name}
                      </option>
                    ))}
                  </select>
                </div>
                <label>
                  {t('notes.text')}
                  <RichEditor value={eText} onChange={setEText} large />
                </label>
                <div className="row">
                  <button
                    type="button"
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
