import type { Annotation } from './useAnnotations'

// An instruction is a comment a person assigned to an assistant: what to
// change, and who changes it. There is no instruction kind on the server; the
// two identities say it, and this file is the one place the UI reads them.
//
// What the reviewer needs on screen is the pairing the server records in
// `instruction_id`: on an instruction, the suggestions that carry it out; on
// a suggestion, the instruction it answers. That is what lets a person read
// each edit next to what was asked, and see an instruction nobody answered.

export function isInstruction(a: Annotation): boolean {
  return a.kind === 'comment' && a.author_kind === 'user' && a.assignee_kind === 'ai_assistant'
}

export function answersTo(rows: Annotation[], instructionId: string): Annotation[] {
  return rows.filter((r) => r.instruction_id === instructionId && !r.deleted_at)
}
