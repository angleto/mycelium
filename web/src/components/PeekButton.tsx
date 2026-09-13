import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import type { components } from '../shared'
import { MarkdownView } from './Markdown'
import { ModalShell } from './ModalShell'
import { TagChip } from './TagChip'

type TaskFull = components['schemas']['TaskOut']
type NoteFull = components['schemas']['NoteOut']

/** What a row knows about itself before anything is fetched. The title
 *  comes from the list payload so the dialog can name itself immediately,
 *  rather than opening as an empty box that fills in. */
export type PeekTarget = { kind: 'task' | 'note'; id: string; title: string }

// Read a row without leaving the list.
//
// The cost this removes is not the rendering, it is the RETURN: opening a
// task from a search meant navigating away and then coming back to a list
// that had to be rebuilt, with the scroll position and the query gone. So
// this never touches the router — it is a dialog over the list, and the
// list is still underneath it when it closes.
//
// Read-only on purpose. Everything here is a view of the entity; the one
// way out to editing is the "Open" link, which is the navigation the user
// was avoiding and now chooses.

export function PeekButton({ target }: { target: PeekTarget }) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  return (
    <>
      <button
        type="button"
        className="btn--ghost btn--sm peek"
        title={t('common.peek')}
        aria-label={t('common.peek')}
        aria-expanded={open}
        onClick={() => setOpen(true)}
      >
        <span aria-hidden="true">👁</span>
      </button>
      {open && <PeekDialog target={target} onClose={() => setOpen(false)} />}
    </>
  )
}

function PeekDialog({ target, onClose }: { target: PeekTarget; onClose: () => void }) {
  const { t } = useTranslation()
  const [task, setTask] = useState<TaskFull | null>(null)
  const [note, setNote] = useState<NoteFull | null>(null)
  const [err, setErr] = useState<string | null>(null)

  // Fetched on open rather than taken from the row: a list payload carries
  // neither a note's body (NoteListOut stops at a preview string) nor a
  // task's checklist (the list endpoints leave it empty so a hundred rows
  // do not fan out a hundred extra queries). Reading the row would show
  // less than the dialog promises, and showing less than promised is the
  // thing that sends the user into the task anyway.
  useEffect(() => {
    let live = true
    void (async () => {
      try {
        const h = workspaceHeader()
        const res =
          target.kind === 'task'
            ? await api.GET('/tasks/{task_id}', {
                params: { header: h, path: { task_id: target.id } },
              })
            : await api.GET('/notes/{note_id}', {
                params: { header: h, path: { note_id: target.id } },
              })
        if (!live) return
        if (res.error) setErr(errMessage(res.error))
        else if (target.kind === 'task') setTask(res.data as TaskFull)
        else setNote(res.data as NoteFull)
      } catch (e) {
        // A throw, not a response: no session to address the workspace
        // with, or the network never answered. Without this the rejection
        // escapes the effect and the dialog sits on "Loading…" forever,
        // which is the one outcome a preview must not have.
        if (live) setErr(errMessage(e))
      }
    })()
    return () => {
      live = false
    }
  }, [target.kind, target.id])

  const to = target.kind === 'task' ? `/tasks/${target.id}` : `/notes/${target.id}`
  const loaded = task ?? note
  return (
    <ModalShell
      title={target.title}
      panelClassName="modal__panel--auto peek__panel"
      onClose={onClose}
    >
      <div className="modal__body peek__body">
        {err ? (
          <p className="err">{err}</p>
        ) : !loaded ? (
          <p className="hint">{t('common.loading')}</p>
        ) : task ? (
          <TaskPeek task={task} />
        ) : note ? (
          <NotePeek note={note} />
        ) : null}
      </div>
      <div className="modal__foot">
        <span className="modal__sp" />
        <Link className="btn--sm" to={to} onClick={onClose}>
          {t('common.peekOpen')}
        </Link>
      </div>
    </ModalShell>
  )
}

function TaskPeek({ task }: { task: TaskFull }) {
  const { t } = useTranslation()
  // Optional in the schema: an older payload, or a list endpoint's empty
  // default, both arrive as "no checklist" rather than as an error.
  const checklist = task.checklist ?? []
  const done = checklist.filter((it) => it.done).length
  return (
    <>
      <p className="peek__meta">
        <span className="chip">{task.state}</span>
        {checklist.length > 0 && (
          <span className="chip">
            {t('tasks.checklistCount', { done, total: checklist.length })}
          </span>
        )}
        {(task.tags ?? []).map((g) => (
          <TagChip key={g.id} name={g.name} color={g.color} kind={g.kind} />
        ))}
      </p>
      {task.description?.trim() ? (
        <MarkdownView text={task.description} parent={{ kind: 'task', id: task.id }} />
      ) : (
        <p className="hint">{t('common.peekEmpty')}</p>
      )}
      {checklist.length > 0 && (
        <ul className="peek__checklist">
          {checklist.map((it) => (
            <li key={it.id} className={it.done ? 'is-done' : undefined}>
              {/* Read-only: this is a view of the item, not the control.
                  Ticking one belongs to the task, which the Open link
                  reaches. */}
              <input type="checkbox" checked={it.done} disabled readOnly />
              {it.text}
            </li>
          ))}
        </ul>
      )}
    </>
  )
}

function NotePeek({ note }: { note: NoteFull }) {
  const { t } = useTranslation()
  // A note's text lives in its parts; `transcript` is what a voice note
  // carries instead. Both are markdown, and either can be absent.
  const parts = note.parts ?? []
  const empty = !note.transcript?.trim() && parts.every((p) => !p.body.trim())
  return (
    <>
      <p className="peek__meta">
        <span className="chip">{note.kind}</span>
        <span className="chip">{note.status}</span>
        {(note.tags ?? []).map((g) => (
          <TagChip key={g.id} name={g.name} color={g.color} kind={g.kind} />
        ))}
      </p>
      {empty ? <p className="hint">{t('common.peekEmpty')}</p> : null}
      {note.transcript?.trim() ? (
        <MarkdownView text={note.transcript} parent={{ kind: 'note', id: note.id }} />
      ) : null}
      {parts.map((p) => (
        <section key={p.id} className="peek__part">
          {p.title ? <h4 className="peek__parttitle">{p.title}</h4> : null}
          <MarkdownView text={p.body} parent={{ kind: 'note', id: note.id }} />
        </section>
      ))}
    </>
  )
}
