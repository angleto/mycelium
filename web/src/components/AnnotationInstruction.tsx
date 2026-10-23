import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { AssigneePicker } from './AssigneePicker'
import type { Annotation } from '../lib/useAnnotations'
import { answersTo, isInstruction } from '../lib/annotationInstruction'

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
          {answers.map((s) => (
            <li key={s.id}>
              <button
                type="button"
                className="anno-instr__link"
                onClick={() => onOpen?.(s.id)}
                title={`${s.original_text ?? ''} → ${s.proposed_text ?? ''}`}
              >
                <del className="anno-del">{excerpt(s.original_text, 30)}</del>{' '}
                <ins className="anno-ins">{excerpt(s.proposed_text, 30)}</ins>
              </button>{' '}
              <span className={`anno-instr__status anno-instr__status--${s.status}`}>
                {t(`annotations.status.${s.status}`, { defaultValue: s.status })}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
