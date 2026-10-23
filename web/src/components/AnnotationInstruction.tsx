import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { AssigneePicker } from './AssigneePicker'
import type { Annotation } from '../lib/useAnnotations'
import { answersTo, diffSpan, isInstruction } from '../lib/annotationInstruction'

// The instruction and its answering suggestions, as a reviewer reads them
// (see lib/annotationInstruction for what an instruction is).

function assigneeName(a: Annotation): string {
  if (a.assignee_label) return a.assignee_label
  if (a.assignee_handle) return `@${a.assignee_handle}`
  return a.assigned_to_identity_id?.slice(0, 8) ?? ''
}

function excerpt(text: string | null | undefined, n = 60): string {
  const s = (text ?? '').replace(/\s+/g, ' ').trim()
  return s.length > n ? `${s.slice(0, n)}…` : s
}

/** One suggestion that carries the instruction out: the edit itself in one
 *  line, and the whole of it a click away, in place. Expanding here rather
 *  than jumping to the suggestion's card, because an accepted suggestion's
 *  card is hidden whenever resolved items are, and a jump to nothing is a
 *  click that does nothing. */
function AnswerItem({ sug }: { sug: Annotation }) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  const d = diffSpan(sug.original_text ?? '', sug.proposed_text ?? '')
  return (
    <li className="anno-instr__answer">
      <button
        type="button"
        className="anno-instr__link"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <span aria-hidden="true">{open ? '▾' : '▸'}</span> {d.before}
        {d.removed && <del className="anno-del">{d.removed}</del>}
        {d.added && <ins className="anno-ins">{d.added}</ins>}
        {d.after}
      </button>{' '}
      <span className={`anno-instr__status anno-instr__status--${sug.status}`}>
        {t(`annotations.status.${sug.status}`, { defaultValue: sug.status })}
      </span>
      {open && (
        <div className="anno-instr__full">
          <div className="anno__diff">
            {sug.original_text && <del className="anno-del">{sug.original_text}</del>}{' '}
            {sug.proposed_text && <ins className="anno-ins">{sug.proposed_text}</ins>}
          </div>
          {sug.body && <div className="muted">{sug.body}</div>}
        </div>
      )}
    </li>
  )
}

/** The assignee line's own controls: change it, remove it, or assign an
 *  unassigned comment. A wrong assignee must be one click from undone, and
 *  the place to undo it is where the assignee is shown. */
function AssigneeActions({
  a,
  onAssign,
}: {
  a: Annotation
  onAssign: (handle: string | null) => Promise<void>
}) {
  const { t } = useTranslation()
  const [picking, setPicking] = useState(false)
  const assigned = !!a.assigned_to_identity_id
  return (
    <>
      {' · '}
      <button type="button" className="anno-instr__link" onClick={() => setPicking((v) => !v)}>
        {assigned ? t('annotations.changeAssignee') : t('annotations.assign')}
      </button>
      {assigned && (
        <>
          {' · '}
          <button type="button" className="anno-instr__link" onClick={() => void onAssign(null)}>
            {t('annotations.removeAssignee')}
          </button>
        </>
      )}
      {picking && (
        <div className="anno-pop__assign">
          <AssigneePicker
            value={a.assignee_handle ?? null}
            onChange={(h) => {
              setPicking(false)
              void onAssign(h)
            }}
          />
        </div>
      )}
    </>
  )
}

export function InstructionLinks({
  a,
  rows,
  onOpen,
  onAssign,
}: {
  a: Annotation
  rows: Annotation[]
  onOpen?: (id: string) => void
  /** Present where the viewer may change the assignment (an open comment). */
  onAssign?: (handle: string | null) => Promise<void>
}) {
  const { t } = useTranslation()
  const actions =
    onAssign && a.kind === 'comment' && a.status === 'open' ? (
      <AssigneeActions a={a} onAssign={onAssign} />
    ) : null

  if (a.kind === 'suggestion') {
    if (!a.instruction_id) return null
    const ins = rows.find((r) => r.id === a.instruction_id)
    return (
      <div className="anno-instr anno-instr--answer">
        <span className="muted">{t('annotations.answersInstruction')}</span>{' '}
        {ins ? (
          <button
            type="button"
            className="anno-instr__link"
            onClick={() => onOpen?.(ins.id)}
            title={ins.body ?? ''}
          >
            “{excerpt(ins.body)}”
          </button>
        ) : (
          <span className="muted">{a.instruction_id.slice(0, 8)}</span>
        )}
      </div>
    )
  }

  if (!a.assigned_to_identity_id) {
    return actions ? (
      <div className="anno-instr">
        <span className="muted">{t('annotations.unassigned')}</span>
        {actions}
      </div>
    ) : null
  }
  const name = assigneeName(a)
  if (!isInstruction(a)) {
    return (
      <div className="anno-instr">
        <span className="muted">{t('annotations.assignedTo', { name })}</span>
        {actions}
      </div>
    )
  }
  const answers = answersTo(rows, a.id)
  return (
    <div className="anno-instr anno-instr--instruction">
      <strong>{t('annotations.instructionFor', { name })}</strong>
      {actions}
      {answers.length === 0 ? (
        <span className="muted"> · {t('annotations.noAnswers')}</span>
      ) : (
        <ul className="anno-instr__answers">
          {answers.map((sug) => (
            <AnswerItem key={sug.id} sug={sug} />
          ))}
        </ul>
      )}
    </div>
  )
}
