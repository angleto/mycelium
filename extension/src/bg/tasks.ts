// Changing a task from the panel.
//
// Every mutation carries the version the panel read. Not a convention: it
// is in the type, so a blind write is unrepresentable. The server answers
// a stale one with the version the row actually has now, which is what
// lets a conflict be a NOTICE -- "this changed while you were looking at
// it", with the fresh row already on screen -- instead of an error the
// person cannot act on.
//
// Nothing here retries a conflict automatically. Replaying a stale write
// is how you overwrite somebody else's change; the person's intent stays
// on the control they used, one key away.

import { call } from './api'
import { entityRoute } from './config'
import { code } from './find'
import type { EntityRow, Result, TaskPatch } from '../shared/protocol'
import type { StoredConnection } from './storage'

interface TaskOut {
  id: string
  title: string
  state_id: string | null
  state: string | null
  priority: number | null
  due_date: string | null
  is_archived: boolean
  version: number
  tags?: { kind: string; name: string }[]
}

interface StateOut {
  id: string
  name: string
  is_terminal: boolean
  is_hidden?: boolean
  ord?: number
}

export function rowFromTask(t: TaskOut): EntityRow {
  return {
    kind: 'task',
    id: t.id,
    title: t.title,
    code: code(t.id),
    route: entityRoute('task', code(t.id)),
    stateId: t.state_id,
    state: t.state,
    priority: t.priority,
    dueDate: t.due_date,
    isArchived: t.is_archived,
    version: t.version,
    projectName: t.tags?.find((g) => g.kind === 'project')?.name ?? null,
  }
}

/** Every state of the workflow this task runs on, not the reachable
 *  subset: the server decides reachability when the move is attempted.
 *  So the control offers them all and a refused transition comes back as
 *  a notice. Guessing reachability here would be a second implementation
 *  of a rule that lives in the workflow. */
export async function states(
  conn: StoredConnection,
  id: string,
): Promise<Result<{ id: string; name: string; isTerminal: boolean }[]>> {
  const res = await call<StateOut[]>(conn, `/tasks/${encodeURIComponent(id)}/states`)
  if (!res.ok) return res
  return {
    ok: true,
    data: res.data
      .filter((s) => !s.is_hidden)
      .sort((a, b) => (a.ord ?? 0) - (b.ord ?? 0))
      .map((s) => ({ id: s.id, name: s.name, isTerminal: s.is_terminal })),
  }
}

export async function patch(
  conn: StoredConnection,
  id: string,
  expectedVersion: number,
  fields: TaskPatch,
  editSessionId?: string,
): Promise<Result<EntityRow>> {
  const body: Record<string, unknown> = { expected_version: expectedVersion }
  if (fields.title !== undefined) body.title = fields.title
  // Importance and urgency are the two axes; priority is DERIVED from
  // them on the server and is not settable. Computing it here would be a
  // second implementation of a rule, and the one that drifts.
  if (fields.importance !== undefined) body.importance = fields.importance
  if (fields.urgency !== undefined) body.urgency = fields.urgency
  // A bare YYYY-MM-DD, deliberately: the server anchors it to the end of
  // that day in the OWNER's timezone. Sending an instant built from this
  // browser's clock would move the deadline for anyone in another zone.
  if (fields.dueDate !== undefined) body.due_date = fields.dueDate
  if (fields.assigneeId !== undefined) body.assignee_id = fields.assigneeId
  if (fields.projectTagId !== undefined) body.project_tag_id = fields.projectTagId
  if (fields.clientTagId !== undefined) body.client_tag_id = fields.clientTagId

  const res = await call<TaskOut>(conn, `/tasks/${encodeURIComponent(id)}`, {
    method: 'PATCH',
    body,
    editSessionId,
  })
  if (!res.ok) return res
  // The response is the canonical row, deliberately: it carries the
  // server-derived fields, so the panel replaces what it had rather than
  // patching its own copy and drifting.
  return { ok: true, data: rowFromTask(res.data) }
}

export async function setState(
  conn: StoredConnection,
  id: string,
  expectedVersion: number,
  stateId: string,
  stateName: string,
): Promise<Result<EntityRow>> {
  const res = await call<{ id: string; version: number }>(
    conn,
    `/tasks/${encodeURIComponent(id)}/state`,
    { method: 'POST', body: { expected_version: expectedVersion, state_id: stateId } },
  )
  if (!res.ok) return res
  // This endpoint answers with the version only, unlike PATCH. The state
  // applied is the one the server accepted by id, so naming it is not a
  // guess; anything else the transition changed is corrected by the next
  // refresh, and that residual is why the row is not treated as
  // canonical here.
  const row = await call<TaskOut>(conn, `/tasks/${encodeURIComponent(id)}`)
  if (row.ok) return { ok: true, data: rowFromTask(row.data) }
  return {
    ok: true,
    data: {
      kind: 'task',
      id,
      title: '',
      code: code(id),
      route: entityRoute('task', code(id)),
      stateId,
      state: stateName,
      version: res.data.version,
    },
  }
}

/** Put the page you are on onto something that already exists.
 *
 *  Append rather than edit, and for a note a NEW PART rather than a body
 *  rewrite: an append cannot truncate what is already there, and a
 *  rewrite can. `dedupe_if_tail_matches` makes it idempotent, which is
 *  what lets the panel offer a retry after a timeout without asking
 *  someone to gamble. */
export async function appendPage(
  conn: StoredConnection,
  id: string,
  kind: 'task' | 'note',
  text: string,
): Promise<Result<void>> {
  const path =
    kind === 'task'
      ? `/tasks/${encodeURIComponent(id)}/description/append`
      : `/notes/${encodeURIComponent(id)}/append`
  const res = await call<unknown>(conn, path, {
    method: 'POST',
    body: { text, dedupe_if_tail_matches: true },
  })
  if (!res.ok) return res
  return { ok: true, data: undefined }
}
