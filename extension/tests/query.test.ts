// The query line's grammar.
//
// The rule these protect is not "the parser works" but "the two surfaces
// mean the same thing by a token". `@name` is a tag on /tasks and it is a
// tag here; `state:` is a key /tasks knows and this surface cannot
// express, so it is dropped and SHOWN rather than quietly turned into
// free text -- because here the server has already cut its answer to the
// top twenty, and a token silently reinterpreted changes which twenty
// came back with nothing to say so.

import { describe, expect, it } from 'vitest'
import { IS_VALUES, SCOPE_SIGILS, parseQuery } from '../src/shared/query'

describe('free text', () => {
  it('is what is left after the structured atoms', () => {
    const q = parseQuery('in:Acme is:task preventivo Bianchi')
    expect(q.text).toBe('preventivo Bianchi')
  })

  it('is the whole query when there is nothing structured', () => {
    expect(parseQuery('preventivo Bianchi').text).toBe('preventivo Bianchi')
  })

  it('survives an empty query', () => {
    const q = parseQuery('   ')
    expect(q.text).toBe('')
    expect(q.unresolved).toEqual([])
  })
})

describe('scope', () => {
  it('reads an in: override without deciding what it names', () => {
    // Resolution needs to know what exists, which the parser does not.
    expect(parseQuery('in:Acme x').scope).toEqual({ needle: 'Acme' })
  })

  it('has a way OUT of a pinned focus', () => {
    expect(parseQuery('in:* x').scope).toEqual({ needle: '*' })
  })

  it('keeps the last in: when a query carries two', () => {
    expect(parseQuery('in:Acme in:Bianchi').scope).toEqual({ needle: 'Bianchi' })
  })
})

describe('kinds and the archive shelf', () => {
  it('searches both kinds by default', () => {
    expect(parseQuery('x').kinds).toEqual(['task', 'note'])
  })

  it('narrows to one', () => {
    expect(parseQuery('is:task x').kinds).toEqual(['task'])
    expect(parseQuery('is:note x').kinds).toEqual(['note'])
  })

  it('takes both when both are asked for', () => {
    expect(parseQuery('is:task is:note x').kinds.sort()).toEqual(['note', 'task'])
  })

  it('leaves the archive shelf out unless asked', () => {
    expect(parseQuery('x').includeArchived).toBe(false)
    expect(parseQuery('is:archived x').includeArchived).toBe(true)
  })

  it('refuses an is: value it does not know rather than guessing', () => {
    const q = parseQuery('is:banana x')
    expect(q.unresolved).toEqual(['is:banana'])
    expect(q.kinds).toEqual(['task', 'note'])
    // The free text still runs: one bad atom does not lose the query.
    expect(q.text).toBe('x')
  })
})

describe('tags mean the same thing on both surfaces', () => {
  it('reads @name and tag:name identically, as /tasks does', () => {
    expect(parseQuery('@legale x').tags).toEqual(['legale'])
    expect(parseQuery('tag:legale x').tags).toEqual(['legale'])
  })

  it('ANDs several', () => {
    expect(parseQuery('@legale @2026').tags).toEqual(['legale', '2026'])
  })

  it('does not mistake an email in the text for a tag', () => {
    // A bare @ with nothing after it is not a reference, and the token
    // is free text like any other.
    expect(parseQuery('@ x').tags).toEqual([])
  })
})

describe('what this surface cannot honour', () => {
  it('drops a key /tasks knows but the server cannot express here', () => {
    // state:, due: and priority: are real /tasks keys. Applying them
    // client-side AFTER the server truncated would mean "of the twenty
    // best matches, the ones due today" instead of "the best matches
    // among the ones due today" -- the same words, a different answer.
    for (const atom of ['state:verify', 'due:today', 'priority:1', 'created:today']) {
      const q = parseQuery(`${atom} x`)
      expect(q.unresolved, atom).toEqual([atom])
      expect(q.text, atom).toBe('x')
    }
  })

  it('drops a union, which no server here expresses', () => {
    expect(parseQuery('a | b').unresolved).toEqual(['|'])
  })

  it('reports a mistyped key rather than searching for it as text', () => {
    const q = parseQuery('stat:verify x')
    expect(q.unresolved).toEqual(['stat:verify'])
    expect(q.text).toBe('x')
  })
})

describe('the declared surface', () => {
  it('adds exactly two sigils of its own', () => {
    expect([...SCOPE_SIGILS]).toEqual(['in', 'is'])
  })

  it('gives is: a closed vocabulary', () => {
    expect([...IS_VALUES]).toEqual(['task', 'note', 'archived'])
  })
})
