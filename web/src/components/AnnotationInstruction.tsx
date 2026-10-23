import { useTranslation } from 'react-i18next'
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

export function InstructionLinks({
  a,
  rows,
  onOpen,
}: {
  a: Annotation
  rows: Annotation[]
  onOpen?: (id: string) => void
}) {
  const { t } = useTranslation()

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

  if (!a.assigned_to_identity_id) return null
  const name = assigneeName(a)
  if (!isInstruction(a)) {
    return (
      <div className="anno-instr">
        <span className="muted">{t('annotations.assignedTo', { name })}</span>
      </div>
    )
  }
  const answers = answersTo(rows, a.id)
  return (
    <div className="anno-instr anno-instr--instruction">
      <strong>{t('annotations.instructionFor', { name })}</strong>
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
