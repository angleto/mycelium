import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { GardenProposalRoute } from './GardenProposalRoute'
import { clearSession, setSession } from '../auth/session'
import i18n from '../i18n'

// What the reading view of a generated note promises (ADR-0075), against a
// double of the API that holds the note's state and changes it the way the
// server does:
// - the whole proposal and the whole text of each source are on the page,
//   which is what the inbox's 280-character preview never gave;
// - the state is said in words, and the controls are the ones that state
//   allows, each named by what it does, found by role and name;
// - every decision echoes the version the page rendered;
// - declining leaves the note on the page, declined, with Approve, Reopen
//   and the permanent removal; reopening brings it back to pending;
// - the permanent removal asks first, names what is lost, and only then
//   writes; afterwards the page goes back to the review list;
// - a note that is not a proposal says so instead of showing an empty page.

const fetchMock = vi.hoisted(() => {
  const fn = vi.fn<(...args: unknown[]) => Promise<Response>>()
  globalThis.fetch = fn as unknown as typeof globalThis.fetch
  return fn
})

const NODE = '00000000-0000-4000-8000-000000000001'
const SRC = '00000000-0000-4000-8000-000000000002'

type State = 'proposed' | 'declined' | 'approved'
let state: State = 'proposed'
let version = 3
let exists = true
// ADR-0076: what an agent attached, served as the API serves it.
let extra: Record<string, unknown> = {}
// What the page asked for: the person reads the sources whole.
const fullText: (string | null)[] = []
const writes: { path: string; body: Record<string, unknown> }[] = []

function json(body: unknown, status = 200): Promise<Response> {
  return Promise.resolve(
    new Response(JSON.stringify(body), {
      status,
      headers: { 'content-type': 'application/json' },
    }),
  )
}

function reading() {
  return {
    note_id: NODE,
    title: 'Distillation · Viaggio a Lisbona',
    body: 'LESSON: the whole distilled text, well past any preview.',
    humus_kind: 'distillation',
    origin_model_id: 'mistral-small',
    review_state: state,
    created_at: '2026-11-20T10:00:00Z',
    decided_at: state === 'proposed' ? null : '2026-11-21T10:00:00Z',
    version,
    sources: [
      {
        note_id: SRC,
        title: 'Viaggio a Lisbona',
        body: 'SOURCE: the whole original note the summary was drawn from.',
        is_archived: true,
        body_chars: 59,
        body_truncated: false,
      },
    ],
    checks: [],
    revises: null,
    revised_by: [],
    ...extra,
  }
}

function answer(): void {
  fetchMock.mockImplementation(async (input: unknown) => {
    const req = input as Request
    const path = new URL(req.url, 'http://t').pathname.replace(/^\/api/, '')
    if (req.method === 'GET' && path === `/garden/review/nodes/${NODE}`) {
      fullText.push(new URL(req.url, 'http://t').searchParams.get('full_text'))
      return exists ? json(reading()) : json({ code: 'note.not_found', detail: 'x' }, 404)
    }
    if (req.method === 'POST' && path.startsWith('/garden/review/')) {
      const body = (await req.json()) as Record<string, unknown>
      writes.push({ path, body })
      if (body.expected_version !== version) {
        return json({ code: 'conflict.stale_version', detail: 'stale' }, 409)
      }
      const verb = path.split('/').pop()
      if (verb === 'remove') {
        exists = false
        return json({ note_id: NODE, removed: true })
      }
      state = verb === 'reject' ? 'declined' : verb === 'reopen' ? 'proposed' : 'approved'
      version += 1
      return json({ note_id: NODE, review_state: state, rejected: state === 'declined', version })
    }
    return json({})
  })
}

let host: HTMLDivElement
let root: Root

async function settle(): Promise<void> {
  await act(async () => {
    await new Promise((r) => setTimeout(r, 0))
  })
}

async function mount(): Promise<void> {
  await act(async () => {
    root.render(
      <MemoryRouter initialEntries={[`/garden/review/${NODE}`]}>
        <Routes>
          <Route path="/garden/review/:noteId" element={<GardenProposalRoute />} />
          <Route path="/garden/review" element={<p data-testid="review-list">list</p>} />
        </Routes>
      </MemoryRouter>,
    )
  })
  await settle()
  await settle()
}

// By role and accessible name, as a person (or a screen reader) finds them:
// the decision controls are the buttons of the group named "Decision".
function buttons(): HTMLElement[] {
  const group = [...host.querySelectorAll<HTMLElement>('[role="group"]')].find(
    (g) => g.getAttribute('aria-label') === i18n.t('gardenProposal.actions'),
  )
  return group ? [...group.querySelectorAll<HTMLElement>('button, [role="button"]')] : []
}
function button(name: string): HTMLElement {
  const el = buttons().find((b) => b.textContent?.trim() === name)
  if (!el) throw new Error(`no button named "${name}"; have ${buttons().map((b) => b.textContent)}`)
  return el
}
const names = () => buttons().map((b) => b.textContent?.trim())

async function click(el: Element): Promise<void> {
  await act(async () => {
    ;(el as HTMLElement).click()
  })
  await settle()
  await settle()
}

beforeEach(async () => {
  await i18n.changeLanguage('it')
  setSession({ token: 't', workspaceId: '00000000-0000-4000-8000-0000000000aa' })
  state = 'proposed'
  version = 3
  exists = true
  extra = {}
  writes.length = 0
  answer()
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
})

afterEach(() => {
  act(() => root.unmount())
  host.remove()
  clearSession()
  fetchMock.mockReset()
})

describe('GardenProposalRoute', () => {
  it('shows the whole proposal beside the whole source, and its state in words', async () => {
    await mount()
    const text = host.textContent ?? ''
    expect(text).toContain('LESSON: the whole distilled text, well past any preview.')
    expect(text).toContain('SOURCE: the whole original note the summary was drawn from.')
    expect(text).toContain(i18n.t('gardenProposal.state.proposed'))
    expect(text).toContain(i18n.t('gardenProposal.archived'))
    expect(host.querySelector(`a[href="/notes/${SRC}"]`)).not.toBeNull()
    expect(names()).toEqual([i18n.t('gardenProposal.approve'), i18n.t('gardenProposal.decline')])
    expect(fullText.every((v) => v === 'true')).toBe(true)
    expect(fullText.length).toBeGreaterThan(0)
  })

  it('declines, stays readable, then reopens, echoing the version each time', async () => {
    await mount()
    await click(button(i18n.t('gardenProposal.decline')))
    expect(writes[0]).toMatchObject({
      path: '/garden/review/reject',
      body: { note_id: NODE, expected_version: 3 },
    })
    expect(host.textContent).toContain(i18n.t('gardenProposal.state.declined'))
    expect(host.textContent).toContain('LESSON: the whole distilled text')
    expect(names()).toEqual([
      i18n.t('gardenProposal.approve'),
      i18n.t('gardenProposal.reopen'),
      i18n.t('gardenProposal.remove'),
    ])

    await click(button(i18n.t('gardenProposal.reopen')))
    expect(writes[1]).toMatchObject({
      path: '/garden/review/reopen',
      body: { note_id: NODE, expected_version: 4 },
    })
    expect(host.textContent).toContain(i18n.t('gardenProposal.state.proposed'))
  })

  it('approves a declined note directly, and then offers to open it', async () => {
    state = 'declined'
    await mount()
    await click(button(i18n.t('gardenProposal.approve')))
    expect(writes[0]).toMatchObject({ path: '/garden/review/approve', body: { expected_version: 3 } })
    expect(host.textContent).toContain(i18n.t('gardenProposal.state.approved'))
    expect(host.querySelector(`a[href="/notes/${NODE}"]`)?.textContent).toBe(
      i18n.t('gardenProposal.openNote'),
    )
  })

  it('removes for good only after a confirmation that names what is lost', async () => {
    state = 'declined'
    await mount()
    await click(button(i18n.t('gardenProposal.remove')))
    expect(writes).toEqual([])
    expect(document.body.textContent).toContain('Distillation · Viaggio a Lisbona')
    const confirm = [...document.querySelectorAll<HTMLElement>('button')].find(
      (b) => b.textContent?.trim() === i18n.t('gardenProposal.removeConfirm'),
    )
    expect(confirm).toBeDefined()
    await click(confirm!)
    expect(writes).toEqual([
      { path: '/garden/review/remove', body: { note_id: NODE, expected_version: 3 } },
    ])
    expect(host.querySelector('[data-testid="review-list"]')).not.toBeNull()
  })

  it('reloads and says so when the proposal changed after it was read', async () => {
    await mount()
    version = 9 // someone else acted on it in the meantime
    await click(button(i18n.t('gardenProposal.decline')))
    expect(host.querySelector('.error')).not.toBeNull()
    // The page now renders the current version, so the next action is valid.
    await click(button(i18n.t('gardenProposal.decline')))
    expect(writes[1].body.expected_version).toBe(9)
  })

  it("shows an agent's check in words, and marks one made on an earlier version", async () => {
    extra = {
      checks: [
        {
          check_id: '00000000-0000-4000-8000-0000000000c1',
          verdict: 'issues',
          summary: 'One claim is not in the source.',
          findings: [
            {
              claim: 'Lisbon was visited in winter',
              status: 'contradicted',
              evidence: 'we left in July',
              comment: '',
            },
            { claim: 'the trip lasted a week', status: 'supported', evidence: 'seven days', comment: '' },
          ],
          checker_identity_id: null,
          checker_model_id: 'claude-opus-5-5',
          note_version: 2,
          stale: true,
          created_at: '2026-11-22T10:00:00Z',
        },
      ],
    }
    await mount()
    const text = host.textContent ?? ''
    expect(text).toContain(i18n.t('gardenProposal.check.verdict.issues'))
    expect(text).toContain(i18n.t('gardenProposal.check.status.contradicted'))
    expect(text).toContain(i18n.t('gardenProposal.check.status.supported'))
    expect(text).toContain('we left in July')
    expect(text).toContain(i18n.t('gardenProposal.check.stale'))
    expect(text).toContain('claude-opus-5-5')
    // A check decides nothing: the decision is still the person's.
    expect(names()).toEqual([i18n.t('gardenProposal.approve'), i18n.t('gardenProposal.decline')])
  })

  it('shows the original a revision replaces, and links a replaced note to its revision', async () => {
    const ORIG = '00000000-0000-4000-8000-0000000000d1'
    extra = {
      origin_model_id: 'external:claude-opus-5-5',
      revises: {
        note_id: ORIG,
        title: 'Distillation · Viaggio a Lisbona',
        origin_model_id: 'mistral-small',
        review_state: 'declined',
        body: 'ORIGINAL: what the small model wrote.',
      },
    }
    await mount()
    expect(host.textContent).toContain(i18n.t('gardenProposal.revises', { model: 'mistral-small' }))
    expect(host.querySelector('details')?.textContent).toContain('ORIGINAL: what the small model wrote.')
    expect(host.querySelector(`a[href="/garden/review/${ORIG}"]`)).not.toBeNull()

    act(() => root.unmount())
    root = createRoot(host)
    const REV = '00000000-0000-4000-8000-0000000000d2'
    state = 'declined'
    extra = {
      revised_by: [
        {
          note_id: REV,
          title: 't',
          origin_model_id: 'external:claude-opus-5-5',
          review_state: 'proposed',
          body: null,
        },
      ],
    }
    await mount()
    expect(host.textContent).toContain(i18n.t('gardenProposal.revisedBy'))
    expect(host.querySelector(`a[href="/garden/review/${REV}"]`)).not.toBeNull()
  })

  it('shows the code to hand to an assistant, and copies the full id', async () => {
    await mount()
    const copy = host.querySelector<HTMLElement>(
      `button[aria-label="${i18n.t('gardenProposal.copyId')}"]`,
    )
    expect(copy?.textContent).toContain(NODE.slice(0, 8))
    expect(copy?.getAttribute('title')).toBe(NODE)
  })

  it('says a note is not a proposal instead of showing an empty page', async () => {
    exists = false
    await mount()
    expect(host.textContent).toContain(i18n.t('gardenProposal.missing'))
    expect(names()).toEqual([])
  })
})
