// The envelope reader, against the two shapes that actually arrive.
//
// The second one is the reason this module is not three lines at a call
// site: FastAPI's own 422 bypasses the domain handler and sends an ARRAY
// of validation objects, and rendering it raw white-screens React with
// "Objects are not valid as a React child". A reader that only handles
// the domain shape looks correct until someone submits a form.

import { describe, expect, it } from 'vitest'
import { errCode, errMessage } from './errors'

const FALLBACK = 'fallback-sentence'

describe('errCode', () => {
  it('reads the stable domain code', () => {
    expect(errCode({ code: 'conflict.stale_version' })).toBe('conflict.stale_version')
  })

  it('is undefined rather than throwing on anything else', () => {
    expect(errCode(undefined)).toBeUndefined()
    expect(errCode(null)).toBeUndefined()
    expect(errCode('boom')).toBeUndefined()
    expect(errCode({})).toBeUndefined()
  })
})

describe('errMessage', () => {
  it('prefers the server prose, which is already localized', () => {
    expect(errMessage({ code: 'x', detail: 'Il task è cambiato.' }, FALLBACK)).toBe(
      'Il task è cambiato.',
    )
  })

  it("renders FastAPI's validation ARRAY as lines, not as [object Object]", () => {
    const body = {
      detail: [
        { loc: ['body', 'title'], msg: 'field required', type: 'missing' },
        { loc: ['body', 'due_date'], msg: 'invalid date', type: 'value_error' },
      ],
    }
    expect(errMessage(body, FALLBACK)).toBe('title: field required; due_date: invalid date')
  })

  it('drops "body" from the path: it names the envelope, not the field', () => {
    expect(errMessage({ detail: [{ loc: ['body'], msg: 'bad' }] }, FALLBACK)).toBe('bad')
  })

  it('survives an array whose entries are unusable', () => {
    expect(errMessage({ detail: [{}, 42, null] }, FALLBACK)).toBe(FALLBACK)
  })

  it('reads a bare object detail that carries a message', () => {
    expect(errMessage({ detail: { msg: 'nope' } }, FALLBACK)).toBe('nope')
  })

  it('falls back to the code before the catalogue sentence', () => {
    expect(errMessage({ code: 'agent.scope_denied' }, FALLBACK)).toBe('agent.scope_denied')
  })

  it('always returns a string, whatever arrived', () => {
    for (const bad of [undefined, null, 0, '', [], {}, { detail: 7 }]) {
      expect(typeof errMessage(bad, FALLBACK)).toBe('string')
    }
    expect(errMessage(undefined, FALLBACK)).toBe(FALLBACK)
  })
})
