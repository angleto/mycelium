// Shared cache for the data needed to render a `@task:<uuid>` mention
// as a chip: the task's title, its current workflow state, and whether
// that state is terminal (so the chip can render the title in
// strikethrough gray when the task is closed). Module-level because
// many markdown chunks may reference the same task on one page; we
// want one round-trip per id, not one per occurrence.

import { authFetch } from '../api/client'

export interface TaskMentionInfo {
  id: string
  title: string | null
  stateId: string | null
  stateName: string | null
  isTerminal: boolean
}

interface WorkflowStateRow {
  id: string
  name: string
  is_terminal: boolean
}

const taskCache = new Map<string, TaskMentionInfo>()
const inflight = new Map<string, Promise<TaskMentionInfo | null>>()

// Workflow states are global per org: id → (name, is_terminal). We
// fetch the default workflow's states once and keep them for the
// session; the SPA already invalidates on logout.
let workflowStatesByIdPromise: Promise<Map<string, WorkflowStateRow>> | null = null

async function loadWorkflowStates(): Promise<Map<string, WorkflowStateRow>> {
  if (!workflowStatesByIdPromise) {
    workflowStatesByIdPromise = (async () => {
      const m = new Map<string, WorkflowStateRow>()
      const wfRes = await authFetch('/workflows')
      if (!wfRes.ok) return m
      const wfs = (await wfRes.json()) as { id: string; is_default: boolean }[]
      const def = wfs.find((w) => w.is_default) ?? wfs[0]
      if (!def) return m
      const stRes = await authFetch(`/workflows/${def.id}/states`)
      if (!stRes.ok) return m
      const states = (await stRes.json()) as WorkflowStateRow[]
      for (const s of states) m.set(s.id, s)
      return m
    })().catch(() => new Map())
  }
  return workflowStatesByIdPromise
}

export async function fetchTaskMention(id: string): Promise<TaskMentionInfo | null> {
  const cached = taskCache.get(id)
  if (cached) return cached
  const pending = inflight.get(id)
  if (pending) return pending
  const promise = (async () => {
    const [statesMap, res] = await Promise.all([
      loadWorkflowStates(),
      authFetch(`/tasks/${id}`),
    ])
    if (!res.ok) return null
    const t = (await res.json()) as { id: string; title: string | null; state_id: string | null }
    const st = t.state_id ? statesMap.get(t.state_id) : undefined
    const info: TaskMentionInfo = {
      id: t.id,
      title: t.title,
      stateId: t.state_id,
      stateName: st?.name ?? null,
      isTerminal: st?.is_terminal ?? false,
    }
    taskCache.set(id, info)
    return info
  })()
  inflight.set(id, promise)
  try {
    return await promise
  } finally {
    inflight.delete(id)
  }
}

export function getCachedTaskMention(id: string): TaskMentionInfo | undefined {
  return taskCache.get(id)
}
