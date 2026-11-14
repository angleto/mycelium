// A person's pinned tasks (migration 0027), as one store every list reads.
//
// The pin control sits on every task row: the list, the board, the recent
// tasks and the pinned list itself. They must agree the moment it is
// pressed, so the state lives here once, outside any one screen, and each
// control subscribes to it. A press updates the store first and the server
// second; if the server refuses, the store goes back and reloads from the
// server, which is the only authority on what is pinned.
//
// The store is per workspace: a pin belongs to a person in a workspace, and
// switching workspace must never show the previous one's pins.
import { useEffect, useSyncExternalStore } from 'react'
import { api, workspaceHeader } from './client'
import { getSession } from '../auth/session'
import type { components } from '../shared'

export type PinnedTask = components['schemas']['TaskOut']

type State = {
  workspace: string | null
  /** Pinned ids, most recently pinned first. */
  ids: string[]
  /** The pinned tasks as the server last returned them, in pin order. */
  tasks: PinnedTask[]
  loaded: boolean
}

let state: State = { workspace: null, ids: [], tasks: [], loaded: false }
const listeners = new Set<() => void>()
let inflight: Promise<void> | null = null

function set(next: Partial<State>): void {
  state = { ...state, ...next }
  for (const l of listeners) l()
}

function subscribe(l: () => void): () => void {
  listeners.add(l)
  return () => listeners.delete(l)
}

function currentWorkspace(): string | null {
  return getSession()?.workspaceId ?? null
}

/** Read the pins from the server. Concurrent callers share one request. */
export function reloadPins(): Promise<void> {
  if (inflight) return inflight
  const ws = currentWorkspace()
  if (!ws) return Promise.resolve()
  inflight = (async () => {
    try {
      const { data } = await api.GET('/tasks', {
        params: { header: workspaceHeader(), query: { pinned: true } },
      })
      // A workspace switch while the request was out: its answer is about
      // the other workspace and is dropped.
      if (currentWorkspace() !== ws) return
      const tasks = data ?? []
      set({ workspace: ws, tasks, ids: tasks.map((t) => t.id), loaded: true })
    } finally {
      inflight = null
    }
  })()
  return inflight
}

/** The store, reloaded when it is empty or belongs to another workspace. */
export function usePins(): State {
  const snap = useSyncExternalStore(subscribe, () => state)
  const ws = currentWorkspace()
  useEffect(() => {
    if (ws && (snap.workspace !== ws || !snap.loaded)) {
      if (snap.workspace !== ws) set({ workspace: ws, ids: [], tasks: [], loaded: false })
      void reloadPins()
    }
  }, [ws, snap.workspace, snap.loaded])
  return snap.workspace === ws ? snap : { workspace: ws, ids: [], tasks: [], loaded: false }
}

/** Pin or unpin. Optimistic, then confirmed by the server's list. */
export async function setPinned(id: string, pinned: boolean): Promise<void> {
  const before = state
  if (pinned) {
    if (!state.ids.includes(id)) set({ ids: [id, ...state.ids] })
  } else {
    set({
      ids: state.ids.filter((x) => x !== id),
      tasks: state.tasks.filter((t) => t.id !== id),
    })
  }
  const { error } = await api.POST(pinned ? '/tasks/pin' : '/tasks/unpin', {
    params: { header: workspaceHeader() },
    body: { task_id: id },
  })
  if (error) set({ ids: before.ids, tasks: before.tasks })
  // The row of a newly pinned task, and the truth after a refusal.
  await reloadPins()
}

/** For tests: forget everything. */
export function resetPinsForTest(): void {
  state = { workspace: null, ids: [], tasks: [], loaded: false }
  inflight = null
}
