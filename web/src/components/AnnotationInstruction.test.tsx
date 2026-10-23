import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { InstructionLinks } from './AnnotationInstruction'
import { isInstruction } from '../lib/annotationInstruction'
import type { Annotation } from '../lib/useAnnotations'
import i18n from '../i18n'

// The reviewer's view of an instruction is the pairing the server records in
// `instruction_id`: on the instruction, the suggestions that carry it out; on
// a suggestion, the instruction it answers. These tests pin that pairing and
// the one rule that decides what an instruction is: a person's comment
// assigned to an assistant, not any assigned comment.

function row(over: Partial<Annotation>): Annotation {
  return {
    id: 'x',
    doc_kind: 'task_description',
    doc_id: 'd',
    kind: 'comment',
    body: '',
    status: 'open',
    version: 1,
    created_at: '2026-10-23T00:00:00Z',
    updated_at: '2026-10-23T00:00:00Z',
    ...over,
  } as Annotation
}

const instruction = row({
  id: 'ins-1',
  body: 'make the day concrete',
  author_kind: 'user',
  assigned_to_identity_id: 'agent',
  assignee_kind: 'ai_assistant',
  assignee_label: 'Claude',
})
const answer = row({
  id: 'sug-1',
  kind: 'suggestion',
  original_text: 'friday',
  proposed_text: 'friday 30 October',
  instruction_id: 'ins-1',
  status: 'accepted',
})
const unrelated = row({ id: 'sug-2', kind: 'suggestion', original_text: 'a', proposed_text: 'b' })

let host: HTMLDivElement
let root: Root

beforeEach(async () => {
  await i18n.changeLanguage('en')
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
})

afterEach(() => {
  act(() => root.unmount())
  host.remove()
})

describe('isInstruction', () => {
  it('is a person comment assigned to an assistant, and nothing else', () => {
    expect(isInstruction(instruction)).toBe(true)
    expect(isInstruction(row({ ...instruction, assignee_kind: 'user' }))).toBe(false)
    expect(isInstruction(row({ ...instruction, author_kind: 'ai_assistant' }))).toBe(false)
    expect(isInstruction(row({ ...instruction, kind: 'suggestion' }))).toBe(false)
  })
})

describe('InstructionLinks', () => {
  it('lists on the instruction only the suggestions that carry it out, and opens one', () => {
    const onOpen = vi.fn()
    act(() =>
      root.render(
        <InstructionLinks a={instruction} rows={[instruction, answer, unrelated]} onOpen={onOpen} />,
      ),
    )
    expect(host.textContent).toContain('Instruction for Claude')
    const items = host.querySelectorAll('.anno-instr__answers li')
    expect(items).toHaveLength(1)
    expect(items[0].textContent).toContain('friday 30 October')
    expect(items[0].textContent).toContain('accepted')
    act(() => (items[0].querySelector('button') as HTMLButtonElement).click())
    expect(onOpen).toHaveBeenCalledWith('sug-1')
  })

  it('says so when nobody has answered the instruction yet', () => {
    act(() => root.render(<InstructionLinks a={instruction} rows={[instruction]} />))
    expect(host.textContent).toContain('no suggestion yet')
  })

  it('shows on a suggestion the instruction it carries out', () => {
    const onOpen = vi.fn()
    act(() =>
      root.render(<InstructionLinks a={answer} rows={[instruction, answer]} onOpen={onOpen} />),
    )
    expect(host.textContent).toContain('Carries out:')
    expect(host.textContent).toContain('make the day concrete')
    act(() => (host.querySelector('button') as HTMLButtonElement).click())
    expect(onOpen).toHaveBeenCalledWith('ins-1')
  })

  it('shows a comment assigned to a person as an assignment, not an instruction', () => {
    const toColleague = row({
      ...instruction,
      assignee_kind: 'user',
      assignee_label: null,
      assignee_handle: 'marta',
    })
    act(() => root.render(<InstructionLinks a={toColleague} rows={[toColleague]} />))
    expect(host.textContent).toContain('Assigned to @marta')
    expect(host.textContent).not.toContain('Instruction')
  })

  it('lets a wrong assignee be removed or changed from the assignee line', async () => {
    const onAssign = vi.fn(async () => {})
    act(() =>
      root.render(<InstructionLinks a={instruction} rows={[instruction]} onAssign={onAssign} />),
    )
    const buttons = [...host.querySelectorAll('button')].map((b) => b.textContent)
    expect(buttons).toEqual(['Change', 'Remove'])
    const remove = [...host.querySelectorAll('button')].find((b) => b.textContent === 'Remove')
    await act(async () => remove?.click())
    expect(onAssign).toHaveBeenCalledWith(null)

    const change = [...host.querySelectorAll('button')].find((b) => b.textContent === 'Change')
    act(() => change?.click())
    expect(host.querySelector('.assignpick')).not.toBeNull()
  })

  it('offers Assign on an open unassigned comment, and nothing on a closed one', () => {
    const plain = row({ id: 'c-1', author_kind: 'user' })
    act(() => root.render(<InstructionLinks a={plain} rows={[plain]} onAssign={async () => {}} />))
    expect(host.textContent).toContain('Not assigned')
    expect([...host.querySelectorAll('button')].map((b) => b.textContent)).toEqual(['Assign'])

    const closed = row({ ...instruction, status: 'resolved' })
    act(() =>
      root.render(<InstructionLinks a={closed} rows={[closed]} onAssign={async () => {}} />),
    )
    expect(host.querySelectorAll('button')).toHaveLength(0)
  })
})
