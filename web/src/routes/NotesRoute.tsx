import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import {
  api,
  authFetch,
  errMessage,
  searchNotesByText,
  workspaceHeader,
} from '../api/client'
import { useSession } from '../auth/useSession'
import { RichEditor } from '../components/RichEditor'
import { NoteListItem } from '../components/NoteListItem'
import { TagPickerGrid } from '../components/TagPickerGrid'
import { VoiceRecorder } from '../components/VoiceRecorder'
import { useFocus } from '../lib/focus'
import type { components } from '../api/schema'

type Note = components['schemas']['NoteListOut']
type Kind = components['schemas']['NoteKind']
type Tag = components['schemas']['TagOut']

const KINDS: Kind[] = ['text', 'voice', 'conversation']

// Notes list: project + tag filters, free-text search, a quick-create
// modal and the canonical-command box. Opening a note navigates to the
// full-page editor (/notes/:id, NoteDetailRoute) — the edit surface is
// no longer a modal. The create modal is the only modal left here.
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
  const [searchParams, setSearchParams] = useSearchParams()
  const [notes, setNotes] = useState<Note[]>([])
  const [loading, setLoading] = useState(true)
  const [tags, setTags] = useState<Tag[]>([])
  const [fTag, setFTag] = useState('')
  // Free-text search: an instant client-side filter over the loaded
  // notes plus a debounced server search (``searchHits``) covering the
  // whole corpus.
  const [q, setQ] = useState('')
  const [searchHits, setSearchHits] = useState<Note[] | null>(null)
  const [searching, setSearching] = useState(false)
  const [cmd, setCmd] = useState('')
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [converting, setConverting] = useState<string | null>(null)
  // Resolve derived-task titles for the list chips without an extra
  // request (filled from the already-loaded task list).
  const [linkTasks, setLinkTasks] = useState<{ id: string; title: string }[]>(
    [],
  )

  // Create draft (the only modal on this route).
  const [creating, setCreating] = useState(false)
  const [cKind, setCKind] = useState<Kind>('text')
  // Voice-note recording buffer: uploaded AFTER the note row is created
  // (the upload endpoint wants a note_id).
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
    const query = q.trim()
    if (query) {
      const hits = await searchNotesByText(query, fTag || undefined)
      setSearchHits(hits)
    }
  }, [fTag, q])

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

  // Debounced server-side search (250ms, abortable).
  useEffect(() => {
    const query = q.trim()
    if (!query) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setSearchHits(null)
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setSearching(false)
      return
    }
    const ac = new AbortController()
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setSearching(true)
    const handle = window.setTimeout(() => {
      void (async () => {
        try {
          const hits = await searchNotesByText(
            query,
            fTag || undefined,
            ac.signal,
          )
          if (ac.signal.aborted) return
          setSearchHits(hits)
        } catch {
          if (!ac.signal.aborted) setSearchHits([])
        } finally {
          if (!ac.signal.aborted) setSearching(false)
        }
      })()
    }, 250)
    return () => {
      window.clearTimeout(handle)
      ac.abort()
    }
  }, [q, fTag])

  // Focus (client/project) filters the list client-side, reactively.
  const candidates = searchHits ?? notes
  const shownNotes = focusActive
    ? candidates.filter((n) => {
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
    : candidates

  // Instant client-side narrowing, but ONLY while the debounced server
  // search is still in flight. Once ``searchHits`` lands, the server has
  // already applied ``q`` over title, part bodies and tag names; running
  // the client predicate on top of it could then only DISCARD hits,
  // because the list projection no longer carries the body (it carries a
  // bounded ``preview``). That AND-ing is how a search silently loses
  // results it correctly found.
  const queryTokens = q.trim().toLowerCase().split(/\s+/).filter(Boolean)
  const visibleNotes =
    queryTokens.length === 0 || searchHits !== null
      ? shownNotes
      : shownNotes.filter((n) => {
          const hay = [
            n.title ?? '',
            n.preview ?? '',
            ...(n.tags ?? []).map((g) => g.name),
          ]
            .join('\n')
            .toLowerCase()
          return queryTokens.every((tok) => hay.includes(tok))
        })

  // Deep links: ``?open=<id>`` redirects to the canonical ``/notes/<id>``
  // page; ``?tag=<id>`` pre-filters; ``?action=new`` opens the create
  // modal (PWA shortcut).
  useEffect(() => {
    const openId = searchParams.get('open')
    const tagId = searchParams.get('tag')
    const action = searchParams.get('action')
    if (!openId && !tagId && action !== 'new') return
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (tagId) setFTag(tagId)
    if (openId) {
      setSearchParams({}, { replace: true })
      navigate(`/notes/${openId}`, { replace: true })
      return
    }
    if (action === 'new') openCreate()
    setSearchParams({}, { replace: true })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams])

  function openCreate() {
    setErr(null)
    setMsg(null)
    setCKind('text')
    setCTitle('')
    setCText('')
    setCProject(focusProject)
    setCreating(true)
  }

  function closeCreate() {
    setCreating(false)
    setCAudioBlob(null)
    setCAudioMime('audio/webm')
    setCAudioSeconds(0)
  }

  // Esc closes the create modal.
  useEffect(() => {
    if (!creating) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') closeCreate()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [creating])

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
    // Voice notes: upload the recorded audio as a note attachment, point
    // ``audio_ref`` at it and trigger an STT run (best-effort).
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
          await api.PATCH('/notes/{note_id}', {
            params: { header: workspaceHeader(), path: { note_id: data.id } },
            body: {
              expected_version: data.version,
              audio_ref: `attachment:${att.id}`,
              audio_seconds: cAudioSeconds,
            },
          })
          await api.POST('/notes/{note_id}/transcribe', {
            params: { header: workspaceHeader(), path: { note_id: data.id } },
            body: { operation_id: `transcribe-${data.id}`, embed: true },
          }).catch(() => undefined)
        }
      } catch {
        /* upload failure leaves the note without audio_ref; the user can
           re-record from the note page. */
      }
    }
    closeCreate()
    // Continue editing on the dedicated note page.
    navigate(`/notes/${data.id}`)
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

  function inheritedExtraTagIds(n: Note): string[] {
    const tagIds = (n.tags ?? []).map((g) => g.id)
    if (n.project_id && !tagIds.includes(n.project_id)) tagIds.push(n.project_id)
    return tagIds
  }

  // Derive a task from a note (kind=derived_from): the note stays alive
  // and the user lands on the new task.
  async function onConvert(n: Note) {
    if (converting !== null) return
    setErr(null)
    setConverting(n.id)
    const label =
      n.title?.trim() ||
      (n.preview ?? '').trim() ||
      n.kind
    const title = label.slice(0, 290)
    const { data, error } = await api.POST('/notes/{note_id}/derive-task', {
      params: { header: workspaceHeader(), path: { note_id: n.id } },
      body: { title, description: null, extra_tag_ids: inheritedExtraTagIds(n) },
    })
    if (error || !data) {
      setConverting(null)
      setErr(errMessage(error))
      return
    }
    setConverting(null)
    navigate(`/tasks/${data.task_id}`)
  }

  // Promote a note into a task (kind=promoted_from): 1:1, the note is
  // marked read-only.
  async function onPromote(n: Note) {
    if (n.promoted_at) return
    if (converting !== null) return
    if (!window.confirm(t('notes.promoteConfirm'))) return
    setErr(null)
    setConverting(n.id)
    const { data, error } = await api.POST('/notes/{note_id}/promote', {
      params: { header: workspaceHeader(), path: { note_id: n.id } },
      body: { title: null },
    })
    if (error || !data) {
      setConverting(null)
      setErr(errMessage(error))
      return
    }
    setConverting(null)
    navigate(`/tasks/${data.task_id}`)
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
    setMsg(t('notes.confirmDelete'))
    await loadNotes()
  }

  // Erase: permanent (note + memory), so it confirms hard.
  async function eraseNote(n: Note) {
    if (
      !window.confirm(t('notes.confirmErase', { title: n.title || n.kind }))
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
    setMsg(t('notes.erased'))
  }

  return (
    <section className="card">
      <h1>{t('notes.title')}</h1>
      <p className="hint">{t('notes.meteredNote')}</p>
      {err && <p className="err">{err}</p>}
      {msg && <p className="ok">{msg}</p>}

      <div className="row">
        <button type="button" onClick={openCreate}>
          {t('notes.newNote')}
        </button>
      </div>
      <div className="row">
        <input
          type="search"
          placeholder={t('notes.search')}
          title={t('notes.searchHint')}
          value={q}
          onChange={(e) => setQ(e.target.value)}
          style={{ flex: 1, minWidth: '12rem' }}
        />
        {searching && (
          <span className="hint" aria-live="polite">
            {t('notes.searching')}
          </span>
        )}
      </div>
      {tags.length > 0 && (
        <div className="row">
          <span className="muted">{t('notes.allTags')}:</span>
          <TagPickerGrid
            tags={tags}
            selected={fTag ? [fTag] : []}
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
      ) : visibleNotes.length === 0 ? (
        <p className="hint">
          {q.trim() ? t('notes.noneMatch') : t('notes.none')}
        </p>
      ) : (
        <ul className="list">
          {visibleNotes.map((n) => (
            <NoteListItem
              key={n.id}
              note={n}
              converting={converting === n.id}
              derivedTaskTitles={(n.derived_task_ids ?? [])
                .map((id) => linkTasks.find((tk) => tk.id === id)?.title)
                .filter((s): s is string => Boolean(s))}
              onOpen={() => navigate(`/notes/${n.id}`)}
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

      {creating && (
        <div
          className="modal__backdrop modal--sheet"
          role="dialog"
          aria-modal="true"
          aria-label={t('notes.newNote')}
        >
          <div className="modal__panel">
            <div className="modal__head">
              <strong>{t('notes.newNote')}</strong>
              <span className="modal__sp" />
              <button
                type="button"
                className="btn--ghost btn--sm"
                onClick={closeCreate}
              >
                {t('notes.close')}
              </button>
            </div>
            <div className="modal__body">
              <div className="row">
                <select
                  value={cKind}
                  onChange={(e) => setCKind(e.target.value as Kind)}
                  aria-label={t('notes.kind')}
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
                  aria-label={t('notes.project')}
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
            <div className="modal__foot">
              <button type="button" onClick={() => void doCreate()}>
                {t('notes.create')}
              </button>
              <button
                type="button"
                className="btn--ghost"
                onClick={closeCreate}
              >
                {t('notes.close')}
              </button>
            </div>
          </div>
        </div>
      )}
    </section>
  )
}
