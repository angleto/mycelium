// A person's pins, as one store per kind of item that every list reads:
// tasks (migration 0027) and notes (migration 0033).
//
// The pin control sits on every row that shows the item: for a task the
// list, the board, the recent tasks and the pinned list itself; for a note
// the notes list, the recent notes and the pinned ones. They must agree the
// moment it is pressed, so the state lives here once, outside any one
// screen, and each control subscribes to it. A press updates the store first
// and the server second; if the server refuses, the store goes back and
// reloads from the server, which is the only authority on what is pinned.
//
// The store is per workspace: a pin belongs to a person in a workspace, and
// switching workspace must never show the previous one's pins.
//
// What else moves a pin. A pin outlives its item being archived or closed,
// but an item put in the bin, restored from it, erased, merged away or sent
// back to review leaves or rejoins the pinned list on the server, and the
// screen that did it reloads only its own list. So the stores watch those requests themselves, on the
// API client, wherever they are made from (the list, the detail page, the
// bin, the review inbox), instead of every such screen having to remember.
//
// The two kinds share the mechanism and differ only in the two requests they
// make and the row they hold, so the mechanism is written once and each kind
// supplies its requests.
import { useEffect, useSyncExternalStore } from 'react'
import type { Middleware } from 'openapi-fetch'
import { api, errMessage, workspaceHeader } from './client'
import { getSession } from '../auth/session'
import type { components, paths } from '../shared'

export type PinnedTask = components['schemas']['TaskOut']
export type PinnedNote = components['schemas']['NoteListOut']
export type PinKind = 'task' | 'note'

export type PinState<T> = {
  workspace: string | null
  /** Pinned ids, most recently pinned first. */
  ids: string[]
  /** The pinned rows as the server last returned them, in pin order. */
  rows: T[]
  loaded: boolean
  /** The last refused press, with the server's reason, until the next one. */
  error: { id: string; message: string } | null
}

type PinIo<T> = {
  /** The person's pinned rows, newest pin first; null when the read failed. */
  read(): Promise<T[] | null>
  /** Set or clear one pin; null when the server accepted it, else the
   *  server's reason, as the person should read it. */
  write(id: string, pinned: boolean): Promise<string | null>
}

export type PinStore<T> = {
  useStore(): PinState<T>
  setPinned(id: string, pinned: boolean): Promise<void>
  reload(): Promise<void>
  /** Reload if anything on screen holds these pins; a store nobody read
   *  has nothing stale in it. */
  invalidate(): void
  resetForTest(): void
}

function currentWorkspace(): string | null {
  return getSession()?.workspaceId ?? null
}

function createPinStore<T extends { id: string }>(io: PinIo<T>): PinStore<T> {
  const empty = (workspace: string | null): PinState<T> => ({
    workspace,
    ids: [],
    rows: [],
    loaded: false,
    error: null,
  })
  let state: PinState<T> = empty(null)
  const listeners = new Set<() => void>()
  let inflight: Promise<void> | null = null
  // A reload asked for while one is out: the one out may have been sent
  // before the change that prompted this one, so a second read follows it
  // instead of the caller sharing a stale answer.
  let queued: Promise<void> | null = null

  function set(next: Partial<PinState<T>>): void {
    state = { ...state, ...next }
    for (const l of listeners) l()
  }

  function subscribe(l: () => void): () => void {
    listeners.add(l)
    return () => listeners.delete(l)
  }

  /** Read the pins from the server. A read already out is followed by one
   *  more, shared by everyone who asks while it waits. */
  function reload(): Promise<void> {
    if (inflight) {
      queued ??= inflight.then(() => {
        queued = null
        return reload()
      })
      return queued
    }
    const ws = currentWorkspace()
    if (!ws) return Promise.resolve()
    inflight = (async () => {
      try {
        const rows = await io.read()
        // A workspace switch while the request was out: its answer is about
        // the other workspace and is dropped.
        if (currentWorkspace() !== ws) return
        // A failed read says nothing about the pins: what the store holds
        // stays, rather than every pin vanishing from the screen.
        if (rows === null) set({ workspace: ws, loaded: true })
        else set({ workspace: ws, rows, ids: rows.map((r) => r.id), loaded: true })
      } finally {
        inflight = null
      }
    })()
    return inflight
  }

  /** The store, reloaded when it is empty or belongs to another workspace. */
  function useStore(): PinState<T> {
    const snap = useSyncExternalStore(subscribe, () => state)
    const ws = currentWorkspace()
    useEffect(() => {
      if (ws && (snap.workspace !== ws || !snap.loaded)) {
        if (snap.workspace !== ws) set(empty(ws))
        void reload()
      }
    }, [ws, snap.workspace, snap.loaded])
    return snap.workspace === ws ? snap : empty(ws)
  }

  /** Pin or unpin. Optimistic, then confirmed by the server's list. */
  async function setPinned(id: string, pinned: boolean): Promise<void> {
    const before = state
    if (pinned) {
      if (!state.ids.includes(id)) set({ ids: [id, ...state.ids] })
    } else {
      set({
        ids: state.ids.filter((x) => x !== id),
        rows: state.rows.filter((r) => r.id !== id),
      })
    }
    const refused = await io.write(id, pinned)
    // The press is undone on the screen when the server refused it, and
    // the reason is kept for the control to show.
    if (refused !== null) {
      set({ ids: before.ids, rows: before.rows, error: { id, message: refused } })
    } else if (state.error) {
      set({ error: null })
    }
    // The row of a newly pinned item, and the truth after a refusal.
    await reload()
  }

  return {
    useStore,
    setPinned,
    reload,
    invalidate: () => {
      if (state.loaded) void reload()
    },
    resetForTest: () => {
      state = empty(null)
      inflight = null
      queued = null
    },
  }
}

export const taskPins = createPinStore<PinnedTask>({
  async read() {
    const { data } = await api.GET('/tasks', {
      params: { header: workspaceHeader(), query: { pinned: true } },
    })
    return data ?? null
  },
  async write(id, pinned) {
    const { error } = await api.POST(pinned ? '/tasks/pin' : '/tasks/unpin', {
      params: { header: workspaceHeader() },
      body: { task_id: id },
    })
    return error ? errMessage(error) : null
  },
})

// The pinned notes come back as one page in pin order. The server caps a
// person's note pins at its page ceiling and refuses a new one past it, so
// this one page is always all of them.
export const notePins = createPinStore<PinnedNote>({
  async read() {
    const { data } = await api.GET('/notes', {
      params: { header: workspaceHeader(), query: { pinned: true, limit: 200 } },
    })
    return data ? data.items : null
  },
  async write(id, pinned) {
    const { error } = await api.POST(pinned ? '/notes/pin' : '/notes/unpin', {
      params: { header: workspaceHeader() },
      body: { note_id: id },
    })
    return error ? errMessage(error) : null
  },
})

// The requests after which the pinned list on the server may differ from the
// store for a reason the store did not cause, each with the kinds of pin it
// moves. Typed against the API description, so a path that stops existing
// fails the build rather than silently never matching. The list is what the
// server does, read from it:
// - an item into or out of the bin, or erased for good;
// - a note merged into another (the source goes to the bin);
// - an atom withdrawn to bring its sources back (it goes back to review,
//   which also drops its pins on the server, and to the bin);
// - the bin emptied.
// Approving or rejecting a proposal moves no pin: a proposal cannot be
// pinned, and a rejected one is already off the list.
export const PIN_MOVING_REQUESTS: ReadonlyArray<{ path: keyof paths; kinds: readonly PinKind[] }> =
  [
    { path: '/notes/{note_id}/delete', kinds: ['note'] },
    { path: '/notes/{note_id}/restore', kinds: ['note'] },
    { path: '/notes/{note_id}/erase', kinds: ['note'] },
    { path: '/notes/merge', kinds: ['note'] },
    { path: '/garden/review/restore-source', kinds: ['note'] },
    { path: '/tasks/{task_id}/delete', kinds: ['task'] },
    { path: '/tasks/{task_id}/restore', kinds: ['task'] },
    { path: '/workspaces/me/trash/empty', kinds: ['note', 'task'] },
  ]

const MATCHERS = PIN_MOVING_REQUESTS.map(({ path, kinds }) => ({
  // A path template to an anchored pattern: each `{param}` is one segment.
  re: new RegExp('^' + path.replace(/\{[^}]+\}/g, '[^/]+') + '$'),
  kinds,
}))

/** The kinds of pin a POST to ``path`` (as sent, without the /api prefix)
 *  may have moved on the server; empty for any other request. */
export function pinKindsMovedBy(path: string): readonly PinKind[] {
  return MATCHERS.find((m) => m.re.test(path))?.kinds ?? []
}

const lifecycle: Middleware = {
  onResponse({ request, response }) {
    if (request.method !== 'POST' || !response.ok) return undefined
    const path = new URL(request.url, 'http://local').pathname.replace(/^\/api/, '')
    for (const kind of pinKindsMovedBy(path)) pinStore(kind).invalidate()
    return undefined
  },
}
api.use(lifecycle)
// In development a hot reload of this module evaluates it again; without
// this, every reload would add one more copy of the watcher to the shared
// client and each delete would re-read the pins once per edit made.
import.meta.hot?.dispose(() => api.eject(lifecycle))

/** The store of one kind, for a control that serves both (the pin button). */
export function pinStore(kind: PinKind): PinStore<{ id: string }> {
  return kind === 'task' ? taskPins : notePins
}

/** The task pins, for the task screens. */
export function usePins(): PinState<PinnedTask> {
  return taskPins.useStore()
}

/** The note pins, for the note screens. */
export function useNotePins(): PinState<PinnedNote> {
  return notePins.useStore()
}

/** For tests: forget everything, both kinds. */
export function resetPinsForTest(): void {
  taskPins.resetForTest()
  notePins.resetForTest()
}
