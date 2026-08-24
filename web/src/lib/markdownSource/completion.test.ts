import { afterEach, describe, expect, it, vi } from 'vitest'
import { EditorSelection, EditorState } from '@codemirror/state'
import { EditorView } from '@codemirror/view'
import {
  CompletionContext,
  acceptCompletion,
  completionStatus,
  currentCompletions,
  startCompletion,
} from '@codemirror/autocomplete'
import { markdownSourceExtensions } from './extensions'
import {
  ENTITY_TRIGGER,
  MENTION_TRIGGER,
  entitySource,
  mentionCompletion,
  mentionSource,
} from './completion'

// The searches are network calls; what is under test here is the trigger
// grammar and what gets INSERTED: bodies written by the retired surface are
// still stored, so a mismatch with what it inserted would leave two
// spellings of the same reference in one workspace.
vi.mock('../mentionSearch', () => ({
  searchCandidates: async (q: string) =>
    q === 'none'
      ? []
      : [{ kind: 'task', id: '0f9c6aaa-1111-2222-3333-444444444444', label: 'Fix ] the parser' }],
  searchEntities: async () => [
    { kind: 'task', id: '91cf6aaa-1111-2222-3333-444444444444', label: 'Roadmap' },
  ],
}))

const views: EditorView[] = []

function ctxFor(doc: string, explicit = false): CompletionContext {
  const state = EditorState.create({
    doc,
    extensions: markdownSourceExtensions({ src: doc, onChange: () => {} }),
    selection: EditorSelection.cursor(doc.length),
  })
  return new CompletionContext(state, doc.length, explicit)
}

afterEach(() => {
  while (views.length) views.pop()?.destroy()
  vi.restoreAllMocks()
})

describe('the trigger grammar', () => {
  it('matches what the user TYPES, not what gets inserted', () => {
    // The `@` typeahead produces `[label](@task:id)`. A trigger keyed on the
    // OUTPUT would only ever fire after the completion had happened.
    expect(MENTION_TRIGGER.test('scrivi @fi')).toBe(true)
    expect(MENTION_TRIGGER.test('@fi')).toBe(true)
    expect(MENTION_TRIGGER.test('mail@example.com')).toBe(false)
    // A space ends it, so an unterminated `@` does not keep matching the
    // rest of the paragraph.
    expect(MENTION_TRIGGER.test('scrivi @ domani')).toBe(false)
    expect(ENTITY_TRIGGER.test('vedi [[road')).toBe(true)
    expect(ENTITY_TRIGGER.test('vedi [road')).toBe(false)
  })
})

describe('what a completion inserts', () => {
  it('escapes the label, so a title with a bracket stays a link', () => {
    const c = mentionCompletion('task', '0f9c6aaa-1111-2222-3333-444444444444', 'Fix ] the parser')
    expect(c.apply).toBe(
      String.raw`[Fix \] the parser](@task:0f9c6aaa-1111-2222-3333-444444444444) `,
    )
  })
})

describe('the mention source', () => {
  it('replaces from the @, not from the whitespace before it', async () => {
    const res = await mentionSource(ctxFor('scrivi @fi'))
    expect(res).not.toBeNull()
    // 'scrivi @fi' -> the `@` is at index 7.
    expect(res?.from).toBe(7)
    expect(res?.options[0].apply).toBe(
      String.raw`[Fix \] the parser](@task:0f9c6aaa-1111-2222-3333-444444444444) `,
    )
  })

  it('stays quiet on a bare @ unless asked explicitly', async () => {
    expect(await mentionSource(ctxFor('scrivi @'))).toBeNull()
    expect(await mentionSource(ctxFor('scrivi @', true))).not.toBeNull()
  })

  it('does not fire inside an email address', async () => {
    expect(await mentionSource(ctxFor('scrivi a mario@rossi'))).toBeNull()
  })
})

describe('the entity source', () => {
  it('inserts the backticked 8-hex prefix, replacing from the [[', async () => {
    const res = await entitySource(ctxFor('vedi [[road'))
    expect(res?.from).toBe(5)
    expect(res?.options[0].apply).toBe('`91cf6aaa` ')
  })
})

describe('pasting markdown source', () => {
  it('lands verbatim, which is what the previous surface could not do', () => {
    // In tiptap a pasted markdown reference arrived as literal text and the
    // serializer escaped it on the way back out, so the stored body held
    // `!\[name\](/attachments/...)` and readers saw the characters instead
    // of the image. Here the document IS the text.
    const view = new EditorView({
      state: EditorState.create({
        doc: '',
        extensions: markdownSourceExtensions({ src: '', onChange: () => {} }),
      }),
    })
    views.push(view)
    const md = '# Titolo\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n![shot](/attachments/x/download)'
    // jsdom has no DataTransfer, so the clipboard payload is attached by
    // hand. `items: []` matters: the editor's own handler looks there for
    // droppable image FILES and must fall through to CodeMirror's text paste
    // when there are none.
    const ev = new Event('paste', { bubbles: true, cancelable: true })
    Object.defineProperty(ev, 'clipboardData', {
      value: { getData: (t: string) => (t === 'text/plain' ? md : ''), items: [], types: ['text/plain'] },
    })
    view.contentDOM.dispatchEvent(ev)
    expect(view.state.sliceDoc()).toBe(md)
  })
})

describe('the popup actually opens', () => {
  // The test that would have caught the bug this file was written around.
  // Both sources returned options and neither ever appeared, because
  // CodeMirror re-filters what a source returns against the text between
  // `from` and the caret -- which starts at the TRIGGER character. It was
  // fuzzy-matching `@fi` against a task called `Fix it`, finding no `@` in
  // the label, and showing nothing. Asserting on the source's return value
  // alone is not enough; the plumbing has to be exercised.
  it('shows the options a source returned, without re-filtering them', async () => {
    const doc = 'scrivi @fi'
    const view = new EditorView({
      state: EditorState.create({
        doc,
        extensions: markdownSourceExtensions({ src: doc, onChange: () => {} }),
        selection: EditorSelection.cursor(doc.length),
      }),
    })
    views.push(view)
    startCompletion(view)
    for (let i = 0; i < 50 && completionStatus(view.state) !== 'active'; i += 1) {
      await new Promise((r) => setTimeout(r, 20))
    }
    expect(completionStatus(view.state)).toBe('active')
    expect(currentCompletions(view.state).map((c) => c.label)).toEqual(['Fix ] the parser'])

    // CodeMirror refuses an accept within `interactionDelay` (75ms) of the
    // popup opening, so a keystroke in flight cannot pick an option the user
    // has not seen yet. Waiting it out here is part of the contract, not a
    // flake workaround.
    await new Promise((r) => setTimeout(r, 120))
    // Accepting REPLACES from the trigger character, leaving the link.
    expect(acceptCompletion(view)).toBe(true)
    expect(view.state.sliceDoc()).toBe(
      String.raw`scrivi [Fix \] the parser](@task:0f9c6aaa-1111-2222-3333-444444444444) `,
    )
  })
})
