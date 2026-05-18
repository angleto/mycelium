import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { mentionLink } from '../lib/mentions'
import { api, errMessage, workspaceHeader } from '../api/client'
import { useSession } from '../auth/useSession'
import { RichEditor } from '../components/RichEditor'
import { MarkdownView } from '../components/Markdown'
import type { components } from '../api/schema'

type Note = components['schemas']['NoteOut']
type Turn = components['schemas']['NoteTurnOut']
type Kind = components['schemas']['NoteKind']

const KINDS: Kind[] = ['text', 'voice', 'conversation']

// Real GET /notes list (newest first; titles auto-derived). The
// canonical command is deterministic/offline (ADR-0021, not metered);
// conversation replies use the LLM (metered) and need a provider.
export function NotesRoute() {
  const { t } = useTranslation()
  const session = useSession()
  const activeId = session?.workspaceId
  const [kind, setKind] = useState<Kind>('text')
  const [title, setTitle] = useState('')
  const [text, setText] = useState('')
  const [cmd, setCmd] = useState('')
  const [created, setCreated] = useState<Note[]>([])
  const [sel, setSel] = useState<Note | null>(null)
  const [eTitle, setETitle] = useState('')
  const [eText, setEText] = useState('')
  const [turns, setTurns] = useState<Turn[]>([])
  const [content, setContent] = useState('')
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [made, setMade] = useState<{ id: string; title: string } | null>(null)
  // Guard the note->task button: one conversion in flight at a time and
  // a note already converted stays disabled, so repeated clicks cannot
  // spawn N duplicate tasks.
  const [converting, setConverting] = useState<string | null>(null)
  const [convertedIds, setConvertedIds] = useState<Set<string>>(new Set())

  const loadNotes = useCallback(async () => {
    const { data } = await api.GET('/notes', {
      params: { header: workspaceHeader() },
    })
    if (data) setCreated(data)
  }, [])

  useEffect(() => {
    let active = true
    void (async () => {
      const { data } = await api.GET('/notes', {
        params: { header: workspaceHeader() },
      })
      if (active && data) setCreated(data)
    })()
    return () => {
      active = false
    }
  }, [activeId])

  async function onCreate(e: FormEvent) {
    e.preventDefault()
    setErr(null)
    const { data, error } = await api.POST('/notes', {
      params: { header: workspaceHeader() },
      body: { kind, title: title || null, text: text || null },
    })
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    await loadNotes()
    setText('')
    setTitle('')
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

  async function onStartConv() {
    setErr(null)
    const { data, error } = await api.POST('/notes/conversations', {
      params: { header: workspaceHeader() },
      body: {},
    })
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    await loadNotes()
  }

  async function editNote(n: Note) {
    setSel(n)
    setMsg(null)
    setErr(null)
    setETitle(n.title ?? '')
    setEText(n.transcript ?? '')
    const { data } = await api.GET('/notes/{note_id}/turns', {
      params: { header: workspaceHeader(), path: { note_id: n.id } },
    })
    setTurns(data ?? [])
  }

  async function saveNote() {
    if (!sel) return
    setErr(null)
    const { error, response } = await api.PATCH('/notes/{note_id}', {
      params: { header: workspaceHeader(), path: { note_id: sel.id } },
      body: {
        expected_version: sel.version,
        title: eTitle,
        text: eText,
      },
    })
    if (response.status === 409) {
      setErr(t('tasks.conflict'))
      await loadNotes()
      return
    }
    if (error) {
      setErr(errMessage(error))
      return
    }
    setMsg(t('notes.saved'))
    setSel(null)
    await loadNotes()
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
    if (sel?.id === n.id) setSel(null)
    await loadNotes()
  }

  async function delNote(n: Note) {
    if (!window.confirm(t('notes.confirmDelete'))) return
    setErr(null)
    const { error } = await api.POST('/notes/{note_id}/delete', {
      params: { header: workspaceHeader(), path: { note_id: n.id } },
      body: { expected_version: n.version },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    if (sel?.id === n.id) setSel(null)
    await loadNotes()
  }

  async function onSend(e: FormEvent) {
    e.preventDefault()
    if (!sel) return
    setErr(null)
    const { error } = await api.POST('/notes/{note_id}/messages', {
      params: { header: workspaceHeader(), path: { note_id: sel.id } },
      body: { content, operation_id: crypto.randomUUID() },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    setContent('')
    await editNote(sel)
  }

  // No backend note->task: compose POST /tasks. The new task carries a
  // resolved back-reference [label](@note:id) so the link is two-way in
  // practice (the task points at the note; notes can @task the task).
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

  async function onErase(n: Note) {
    setErr(null)
    const { error } = await api.POST('/notes/{note_id}/erase', {
      params: { header: workspaceHeader(), path: { note_id: n.id } },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    setCreated((xs) => xs.filter((x) => x.id !== n.id))
    if (sel?.id === n.id) setSel(null)
    setMsg(t('notes.erased'))
  }

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

      <form onSubmit={(e) => void onCreate(e)}>
        <div className="row">
          <select value={kind} onChange={(e) => setKind(e.target.value as Kind)}>
            {KINDS.map((k) => (
              <option key={k} value={k}>
                {k}
              </option>
            ))}
          </select>
          <input
            placeholder={t('notes.noteTitle')}
            value={title}
            onChange={(e) => setTitle(e.target.value)}
          />
        </div>
        <label>
          {t('notes.text')}
          <RichEditor value={text} onChange={setText} />
        </label>
        <div className="row">
          <button type="submit">{t('notes.create')}</button>
          <button type="button" onClick={() => void onStartConv()}>
            {t('notes.startConv')}
          </button>
        </div>
      </form>
      <p className="hint">{t('notes.kindsHint')}</p>

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

      <h2>{t('notes.yours')}</h2>
      {created.length === 0 ? (
        <p className="hint">{t('notes.none')}</p>
      ) : (
        <ul className="list">
          {created.map((n) => (
            <li key={n.id}>
              {n.title || n.kind}{' '}
              <span className="muted">· {n.kind} · {n.status}</span>
              <button
                type="button"
                className="btn--ghost btn--sm"
                onClick={() => void editNote(n)}
              >
                {t('notes.edit')}
              </button>
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
                {t('notes.delete')}
              </button>
              <button
                type="button"
                className="btn--ghost btn--sm"
                title={t('notes.eraseHint')}
                onClick={() => void onErase(n)}
              >
                {t('notes.erase')}
              </button>
            </li>
          ))}
        </ul>
      )}

      {sel && sel.kind !== 'conversation' && (
        <div className="card" style={{ marginTop: '0.6rem' }}>
          <h2>{t('notes.editing')}</h2>
          <input
            placeholder={t('notes.titlePlaceholder')}
            value={eTitle}
            onChange={(e) => setETitle(e.target.value)}
          />
          <label>
            {t('notes.text')}
            <RichEditor value={eText} onChange={setEText} />
          </label>
          <div className="row">
            <button type="button" onClick={() => void saveNote()}>
              {t('notes.saveNote')}
            </button>
            <button
              type="button"
              className="btn--ghost"
              onClick={() => setSel(null)}
            >
              {t('notes.cancel')}
            </button>
          </div>
        </div>
      )}

      {sel && sel.kind === 'conversation' && (
        <div>
          <h2>
            {t('notes.turns')}: {sel.title || sel.kind}
          </h2>
          <ul className="list">
            {turns.map((tr) => (
              <li key={tr.id}>
                <strong>{tr.role}:</strong> <MarkdownView text={tr.content} />
              </li>
            ))}
          </ul>
          <form onSubmit={(e) => void onSend(e)} className="row">
            <input
              required
              placeholder={t('notes.message')}
              value={content}
              onChange={(e) => setContent(e.target.value)}
            />
            <button type="submit">{t('notes.send')}</button>
          </form>
        </div>
      )}
    </section>
  )
}
