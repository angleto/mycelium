// The three lists the side panel keeps open beside you.
//
// None of them names a workflow state, and that is the constraint that
// chose them. The state machine is per workspace, so a panel asking for
// "in progress" would hold a second definition of it and break the day
// somebody renames a state. Openness and ordering the server already
// understands, so the panel asks in those terms and lets the workspace
// decide what "open" means.
//
// Each is a bounded top-N by an explicit order, which is not pagination:
// nobody pages through these, and asking for five rows costs five rows.
// Before the read parameters existed, the only way to build any of them
// was to download every task in the workspace and cut the list here.

import { call } from './api'
import { rowFromTask } from './tasks'
import type { EntityRow, Result, Sections } from '../shared/protocol'
import type { StoredConnection } from './storage'

const PER_SECTION = 5

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

/** End of today in the reader's own timezone, as the instant the server
 *  compares against. "Overdue" is a calendar question, and answering it
 *  in UTC moves the boundary by a working morning for half the world. */
function endOfToday(): string {
  const d = new Date()
  d.setHours(23, 59, 59, 999)
  return d.toISOString()
}

async function list(
  conn: StoredConnection,
  query: Record<string, string>,
): Promise<EntityRow[]> {
  const res = await call<TaskOut[]>(conn, '/tasks', { query })
  return res.ok ? res.data.map(rowFromTask) : []
}

export async function sections(conn: StoredConnection): Promise<Result<Sections>> {
  // Three bounded reads in parallel: they are independent, and running
  // them in series would make the panel's first paint the sum of three
  // round trips rather than the slowest one.
  const [due, pressing, touched] = await Promise.all([
    list(conn, {
      open_only: 'true',
      due_before: endOfToday(),
      order_by: 'due_date',
      limit: String(PER_SECTION),
    }),
    list(conn, {
      open_only: 'true',
      order_by: 'priority',
      limit: String(PER_SECTION),
    }),
    list(conn, {
      order_by: 'updated_at',
      order_desc: 'true',
      limit: String(PER_SECTION),
    }),
  ])
  const dueIds = new Set(due.map((r) => r.id))
  return {
    ok: true,
    data: {
      due,
      // A task that is both overdue and the most pressing appears once,
      // in the section that is about acting today.
      pressing: pressing.filter((r) => !dueIds.has(r.id)),
      touched,
    },
  }
}
