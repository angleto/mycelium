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

/** The part of a suggestion that actually changes, with a little context.
 *
 *  Two truncations of the original and the proposed text side by side say
 *  nothing when the edit is in the middle: both start with the same words, so
 *  the reviewer reads the same prefix twice. Trimming the common prefix and
 *  suffix leaves the edit itself, which is what a one-line summary is for. */
export function diffSpan(
  original: string,
  proposed: string,
  context = 24,
): { before: string; removed: string; added: string; after: string } {
  let p = 0
  const max = Math.min(original.length, proposed.length)
  while (p < max && original[p] === proposed[p]) p++
  let q = 0
  while (q < max - p && original[original.length - 1 - q] === proposed[proposed.length - 1 - q]) q++
  const head = original.slice(0, p)
  const tail = original.slice(original.length - q)
  return {
    before: head.length > context ? `…${head.slice(head.length - context)}` : head,
    removed: original.slice(p, original.length - q),
    added: proposed.slice(p, proposed.length - q),
    after: tail.length > context ? `${tail.slice(0, context)}…` : tail,
  }
}

/** The threads the panel shows. Resolve is an act on the THREAD, and the
 *  thread follows its root: a resolved root hides with all its replies unless
 *  resolved items are shown, and a visible root shows every reply. A thread
 *  that stayed visible while any reply was open made resolving an
 *  instruction with an agent's reply under it look like a click that did
 *  nothing. */
export function visibleRoots(rows: Annotation[], includeResolved: boolean): Annotation[] {
  return rows.filter((r) => !r.parent_id && (includeResolved || r.status === 'open'))
}
