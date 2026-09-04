import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, useEffect } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { clearSession, setActiveWorkspace, setSession } from '../auth/session'
import type { components } from '../shared'
import {
  __resetRunningTimers,
  refreshRunning,
  useRunningTimers,
} from './useRunningTimer'

// This store answers one question for three surfaces at once (the
// top-bar chip, every TaskTimer, the Time view's card), and the way it
// failed was to answer "nothing is running" to a question it had not
// managed to ask: openapi-fetch resolves a non-2xx as `{ error }`
// rather than throwing, so a 500 on the poll emptied the store and the
// chip vanished while the server still had the timer open. Both
// directions are asserted below — a failed read must not clear, and a
// successful empty read must — plus the tenant and ordering rules that
// decide WHICH answer is allowed to land.

type Entry = components['schemas']['TimeEntryOut']

// A complete TimeEntryOut, as the server would send it: the store keeps
// whatever the contract carries, so a partial object here would let the
// test pass on a payload production never produces.
function entry(id: string, taskId: string): Entry {
  return {
    id,
    task_id: taskId,
    user_id: '00000000-0000-4000-8000-000000000001',
    started_at: '2026-09-01T08:00:00+00:00',
    ended_at: null,
    duration_seconds: null,
    accumulated_seconds: 0,
    resumed_at: '2026-09-01T08:00:00+00:00',
    source: 'timer',
    executor_kind: 'human',
    billable: true,
    parallel: false,
    rate_snapshot: null,
    currency: 'EUR',
    memo: null,
    version: 1,
    task_title: 'Write the report',
  }
}

const E1 = entry(
  '11111111-1111-4111-8111-111111111111',
  '22222222-2222-4222-8222-222222222222',
)

// One request the test holds open until it decides what the server said.
// Deferred rather than pre-canned because half of what is asserted here
// is ORDERING: which read is on the wire when a switch or a mutation
// arrives, and which answer is then allowed to publish.
type Pending = {
  url: string
  workspace: string | null
  reply: (status: number, body?: unknown) => void
}

// The api client resolves `fetch` ONCE, when openapi-fetch creates it at
// import time, so the stub has to be installed before `api/client` is
// evaluated: hoisted runs before the imports above it.
const pending = vi.hoisted(() => {
  const queue: Pending[] = []
  globalThis.fetch = ((input: RequestInfo | URL) => {
    const req = input instanceof Request ? input : null
    return new Promise<Response>((resolve) => {
      queue.push({
        url: req ? req.url : String(input),
        workspace: req?.headers.get('x-workspace-id') ?? null,
        reply: (status, body) =>
          resolve(
            new Response(JSON.stringify(body ?? []), {
              status,
              headers: { 'content-type': 'application/json' },
            }),
          ),
      })
    })
  }) as typeof globalThis.fetch
  return queue
})

/** Answer the oldest read on the wire, waiting for it to get there: a
 * read is issued a microtask or two after the call that asks for it (the
 * client's request middleware is async), so "no read yet" here means
 * "not yet", not "never". */
async function answer(status: number, body?: unknown): Promise<void> {
  for (let i = 0; i < 50 && pending.length === 0; i++) {
    await act(async () => {
      await new Promise((r) => setTimeout(r, 1))
    })
  }
  const p = pending.shift()
  if (!p) throw new Error('no read reached the wire')
  await act(async () => {
    p.reply(status, body)
    await new Promise((r) => setTimeout(r, 0))
  })
}

let seen: { known: boolean; running: Entry[] } = { known: false, running: [] }

// Records what the hook hands a mounted consumer. Written from an
// effect, not during render: assertions run after act() has flushed the
// commit, so this is the same value the surface just rendered.
function Probe() {
  const { known, running } = useRunningTimers()
  useEffect(() => {
    seen = { known, running }
  })
  return null
}

let host: HTMLDivElement
let root: Root

// Mounting asks the store for a first read, and settles so the request
// has reached the wire before the test asserts on it.
async function mount(): Promise<void> {
  await act(async () => {
    root.render(<Probe />)
    await new Promise((r) => setTimeout(r, 0))
  })
}

beforeEach(async () => {
  // Signing in is itself a workspace change, and the store reacts to it
  // with a read: let that one reach the wire, then reset, so each test
  // starts from "nothing read yet" with an empty wire.
  setSession({ token: 'test-token', workspaceId: 'ws-a' })
  await new Promise((r) => setTimeout(r, 0))
  __resetRunningTimers()
  pending.length = 0
  seen = { known: false, running: [] }
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
})

afterEach(() => {
  act(() => {
    root.unmount()
  })
  host.remove()
  // Signing out is a workspace change too; with no session the read it
  // triggers never reaches the network.
  clearSession()
  __resetRunningTimers()
  pending.length = 0
})

describe('useRunningTimers', () => {
  it('does not report an idle state before the first read has landed', async () => {
    await mount()
    // The mount asks; nothing has answered yet. Reporting an empty list
    // as fact here is what let the Time view print "no timer running"
    // over a workspace it had not read.
    expect(pending).toHaveLength(1)
    expect(seen.known).toBe(false)
    expect(seen.running).toEqual([])
  })

  it('keeps the running timer when a read fails with an HTTP status', async () => {
    await mount()
    await answer(200, [E1])
    expect(seen.known).toBe(true)
    expect(seen.running).toHaveLength(1)

    const done = refreshRunning()
    await answer(500, { detail: 'boom' })
    await done

    // The server still has the entry open; a 502 from a proxy, a 500
    // from the title resolution or an expired session must not be
    // rendered as "you are not tracking anything".
    expect(seen.running).toHaveLength(1)
    expect(seen.known).toBe(true)
  })

  it('clears the timer when the server says nothing is running', async () => {
    await mount()
    await answer(200, [E1])

    const done = refreshRunning()
    await answer(200, [])
    await done

    // The other direction of the same rule: a stop performed on another
    // device is a successful read, and it must land.
    expect(seen.running).toEqual([])
    expect(seen.known).toBe(true)
  })

  it('drops the previous tenant on a workspace switch and reads again', async () => {
    await mount()
    await answer(200, [E1])
    expect(seen.running).toHaveLength(1)

    await act(async () => {
      setActiveWorkspace('ws-b')
      await new Promise((r) => setTimeout(r, 0))
    })

    // Not "no timer in ws-b" — not read yet in ws-b. The entry belongs
    // to ws-a and rendering it here would be another tenant's timer in
    // this one's chip.
    expect(seen.known).toBe(false)
    expect(seen.running).toEqual([])
    expect(pending).toHaveLength(1)
    expect(pending[0].workspace).toBe('ws-b')
    expect(pending[0].url).toContain('/time/running')
  })

  it('discards a read that lands after the workspace switched under it', async () => {
    await mount()
    expect(pending).toHaveLength(1)

    await act(async () => {
      setActiveWorkspace('ws-b')
      await new Promise((r) => setTimeout(r, 0))
    })
    // The ws-a read is still on the wire; answer it now.
    await answer(200, [E1])

    expect(seen.known).toBe(false)
    expect(seen.running).toEqual([])
    // …and the re-read for the new workspace went out behind it.
    expect(pending).toHaveLength(1)
    expect(pending[0].workspace).toBe('ws-b')
  })

  it('reconciles a mutation against a read issued after it, not before', async () => {
    await mount()
    // A poll left before the start/stop the caller is reconciling.
    expect(pending).toHaveLength(1)

    const done = refreshRunning()
    await answer(200, []) // the pre-mutation answer: nothing running
    // A second read went out behind it — answering it is what settles
    // the reconcile.
    await answer(200, [E1])
    await done

    // Joining the in-flight read would have reconciled the just-started
    // timer to "nothing is running" and left every surface empty until
    // the next poll.
    expect(seen.running).toHaveLength(1)
    expect(seen.known).toBe(true)
  })
})

// The rules above only hold where they are the only rules: they live in
// ONE module, and every surface reads that module. The defect they were
// written for did not come from getting them wrong, it came from a
// second copy of the same state elsewhere (the Time view polled
// /time/running itself and reconciled only its own copy, so a timer
// started there was missing from the top-bar chip until an unrelated
// backstop fired). No unit test of this module can see that copy
// appear; this one can.
describe('one reader of /time/running', () => {
  const ENDPOINT = '/time/running'
  // The generated schema DECLARES the path (it describes the API); the
  // store reads it, and this file names it to make that assertion.
  const ALLOWED = new Set([
    '../shared/schema.d.ts',
    './useRunningTimer.ts',
    './useRunningTimer.test.tsx',
  ])

  it('is the store, and no surface reads the endpoint behind its back', () => {
    // Read through Vite's own glob rather than node:fs: `src` is
    // typechecked without node types on purpose (tsconfig.app.json), and
    // this assertion is not worth loosening that for.
    const sources = import.meta.glob('../**/*.{ts,tsx}', {
      query: '?raw',
      import: 'default',
      eager: true,
    }) as Record<string, string>
    const readers = Object.entries(sources)
      .filter(([, text]) => text.includes(ENDPOINT))
      .map(([path]) => path)
      .filter((path) => !ALLOWED.has(path))
    expect(readers).toEqual([])
  })
})
