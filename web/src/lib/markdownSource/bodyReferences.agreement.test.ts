import { describe, expect, it } from 'vitest'
import { syntaxTree } from '@codemirror/language'
import { EditorState } from '@codemirror/state'
import { markdownSourceExtensions } from './extensions'
import { collectHits } from './entityChips'
import { parseMentionHref } from '../mentions'

// ADR-0073 decision 2: what a reference is has one rule on the server
// (services/reference_extract.py) and its own recogniser here, because the
// editor decorates text that has not been saved. The two read one corpus, and
// a body they read differently fails this test rather than drifting.
//
// Bare codes are not decorated by the editor (only the server reads them, on
// the old normaliser's gate), so this side checks chips and mentions.

type Expected = [string, string, number] | [string, string, number, string]
type Case = { name: string; body: string; expected: Expected[] }

const raws = import.meta.glob('../../shared/fixtures/body_references.json', {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>
const corpus = JSON.parse(Object.values(raws)[0]) as { cases: Case[] }

function stateFor(body: string): EditorState {
  return EditorState.create({
    doc: body,
    extensions: markdownSourceExtensions({ src: body, onChange: () => {} }),
  })
}

function recognised(body: string): Expected[] {
  const state = stateFor(body)
  const found: { at: number; mention: boolean; ref: Expected }[] = []
  for (const h of collectHits(state)) {
    found.push({ at: h.from, mention: false, ref: ['chip', h.prefix, 0] })
  }
  syntaxTree(state).iterate({
    enter: (node) => {
      if (node.name !== 'Link') return
      const url = node.node.getChild('URL')
      if (!url) return
      const m = parseMentionHref(state.doc.sliceString(url.from, url.to))
      if (m) {
        found.push({
          at: node.from,
          mention: true,
          ref: ['mention', m.id.toLowerCase(), 0, m.kind],
        })
      }
    },
  })
  // Reading order, mentions before a chip in their own label, then ordinals
  // per (syntax, token): the server's ordering.
  found.sort((a, b) => a.at - b.at || Number(b.mention) - Number(a.mention))
  const seen = new Map<string, number>()
  return found.map(({ ref }) => {
    const key = `${ref[0]}|${ref[1]}`
    const n = seen.get(key) ?? 0
    seen.set(key, n + 1)
    const out = [...ref] as Expected
    out[2] = n
    return out
  })
}

describe('body references agree with the server extractor', () => {
  it('reads the shared corpus', () => {
    expect(corpus.cases.length).toBeGreaterThan(20)
  })
  for (const c of corpus.cases) {
    it(c.name, () => {
      const expected = c.expected.filter((e) => e[0] !== 'bare')
      expect(recognised(c.body)).toEqual(expected)
    })
  }
})
