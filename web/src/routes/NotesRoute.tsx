import { useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { mentionLink } from '../lib/mentions'
import { api, errMessage, workspaceHeader } from '../api/client'
import { RichEditor } from '../components/RichEditor'
import { MarkdownView } from '../components/Markdown'
import type { components } from '../api/schema'

type Note = components['schemas']['NoteOut']
type Turn = components['schemas']['NoteTurnOut']
type Kind = components['schemas']['NoteKind']

const KINDS: Kind[] = ['text', 'voice', 'conversation']

// No GET /notes list endpoint exists (tracked API gap), so created
// notes are kept session-local. The canonical command is deterministic
// and offline (ADR-0021, not metered); conversation replies use the
// LLM (metered) and need a provider configured on the server.
export function NotesRoute() {
  const { t } = useTranslation()
  const [kind, setKind] = useState<Kind>('text')
  const [title, setTitle] = useState('')
  const [text, setText] = useState('')
  const [cmd, setCmd] = useState('')
  const [created, setCreated] = useState<Note[]>([])
  const [sel, setSel] = useState<Note | null>(null)
  const [turns, setTurns] = useState<Turn[]>([])
  const [content, setContent] = useState('')
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [made, setMade] = useState<{ id: string; title: string } | null>(null)

  function remember(n: Note) {
    setCreated((xs) => [n, ...xs.filter((x) => x.id !== n.id)])
  }

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
    remember(data)
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
    remember(data)
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
    remember(data)
  }

  async function openNote(n: Note) {
    setSel(n)
    setMsg(null)
    setErr(null)
    const { data } = await api.GET('/notes/{note_id}/turns', {
      params: { header: workspaceHeader(), path: { note_id: n.id } },
    })
    setTurns(data ?? [])
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
    await openNote(sel)
  }

  // No backend note->task: compose POST /tasks. The new task carries a
  // resolved back-reference [label](@note:id) so the link is two-way in
  // practice (the task points at the note; notes can @task the task).
  async function onConvert(n: Note) {
    setErr(null)
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
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
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
                onClick={() => void openNote(n)}
              >
                {t('notes.open')}
              </button>
              <button
                type="button"
                className="btn--sm"
                onClick={() => void onConvert(n)}
              >
                {t('notes.toTask')}
              </button>
              <button
                type="button"
                className="btn--ghost btn--sm"
                onClick={() => void onErase(n)}
              >
                {t('notes.erase')}
              </button>
            </li>
          ))}
        </ul>
      )}

      {sel && (
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
          {sel.kind === 'conversation' && (
            <form onSubmit={(e) => void onSend(e)} className="row">
              <input
                required
                placeholder={t('notes.message')}
                value={content}
                onChange={(e) => setContent(e.target.value)}
              />
              <button type="submit">{t('notes.send')}</button>
            </form>
          )}
        </div>
      )}
    </section>
  )
}
