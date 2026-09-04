// The on/off switch, checked where it is a guarantee.
//
// The worker is the only thing in this package that can reach the
// network, so refusing at the dispatcher provably stops every outbound
// request. That property is the reason the switch is worth having as a
// checkbox rather than as a preference nobody trusts -- and it only holds
// if the check happens BEFORE the handler table, so an operation added
// later inherits the refusal instead of an exemption.

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ALWAYS_AVAILABLE } from '../src/shared/protocol'
import type { OperationName } from '../src/shared/protocol'
import { installFakeChrome, type FakeChrome } from './fake-chrome'

let fake: FakeChrome

async function load() {
  vi.resetModules()
  fake = installFakeChrome()
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => new Response('[]', { headers: { 'content-type': 'application/json' } })),
  )
  await import('../src/bg/index')
}

type Reply = { ok: boolean; error?: { code: string } }

describe('the operation seam', () => {
  beforeEach(load)

  it('is ON for a fresh install: an absent setting means enabled', async () => {
    expect(await fake.message({ op: 'switch/get', payload: undefined })).toEqual({
      ok: true,
      data: true,
    })
  })

  it('refuses every ordinary operation while off, with its own code', async () => {
    await fake.message({ op: 'switch/set', payload: { on: false } })
    const reply = (await fake.message({ op: 'find/query', payload: { q: 'x', gen: 1 } })) as Reply
    expect(reply.ok).toBe(false)
    // A distinct code, not "forbidden": refused by the HOST and refused
    // by the SERVER imply opposite next actions -- turn it on, versus go
    // and ask for scope.
    expect(reply.error?.code).toBe('disabled')
  })

  it('keeps the switch and the connection reachable while off', async () => {
    await fake.message({ op: 'switch/set', payload: { on: false } })
    for (const op of ALWAYS_AVAILABLE) {
      const reply = (await fake.message({ op, payload: undefined })) as Reply
      expect(reply.error?.code, op).not.toBe('disabled')
    }
  })

  it('tears down the context menus with it, and puts them back', async () => {
    // Chrome persists menus across restarts, so an extension turned off
    // would otherwise keep live entries that answer "disabled" when
    // clicked -- which reads as broken rather than as off.
    await fake.message({ op: 'switch/set', payload: { on: true } })
    expect(fake.recorded.menus.length).toBeGreaterThan(0)
    await fake.message({ op: 'switch/set', payload: { on: false } })
    expect(fake.recorded.menus).toEqual([])
  })

  it('refuses an ordinary operation with no credential, distinctly from off', async () => {
    const reply = (await fake.message({ op: 'find/query', payload: { q: 'x', gen: 1 } })) as Reply
    expect(reply.ok).toBe(false)
    expect(reply.error?.code).toBe('disconnected')
  })

  it('answers an unknown operation instead of throwing at the caller', async () => {
    const reply = (await fake.message({ op: 'not/an/op', payload: undefined })) as Reply
    expect(reply.ok).toBe(false)
    expect(reply.error?.code).toBe('not_found')
  })

  it('never lets a connection row carry the secret across the seam', async () => {
    const { storage } = await import('../src/bg/storage')
    await storage.putConnection({
      workspaceId: 'ws-1',
      workspaceName: 'Personal',
      assistantId: 'a-1',
      scope: [],
      secret: 'mycelium_at_secret',
    })
    const reply = (await fake.message({ op: 'conn/list', payload: undefined })) as {
      data: Record<string, unknown>[]
    }
    expect(reply.data).toHaveLength(1)
    expect(JSON.stringify(reply.data)).not.toContain('mycelium_at_secret')
    expect(reply.data[0]).not.toHaveProperty('secret')
  })

  it('has a handler for every operation the protocol declares', async () => {
    // Fail-closed is only a safety net if nothing is relying on it: a
    // declared operation with no handler answers "not_found" at run
    // time, which looks like a typo at the call site rather than a gap
    // here.
    const declared: OperationName[] = [
      'switch/get', 'switch/set',
      'conn/list', 'conn/begin', 'conn/forget', 'conn/self',
      'scope/get', 'scope/set', 'scope/clients', 'scope/projects',
      'find/query', 'find/recents', 'find/opened',
      'task/states', 'task/patch', 'task/setState', 'task/attachPage',
      'capture/context', 'capture/screenshot', 'capture/create',
      'log/sinceYouLeft',
    ]
    for (const op of declared) {
      const reply = (await fake.message({ op, payload: undefined })) as Reply
      expect(reply.error?.code, op).not.toBe('not_found')
    }
  })
})
