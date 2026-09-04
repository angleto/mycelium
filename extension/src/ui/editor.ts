// The expanded row: changing a task without leaving the panel.
//
// Pessimistic, version-guarded, and busy PER CONTROL rather than per row.
// The prior art dims the whole row while any write is in flight, which is
// a regression the moment two fields are independent: setting a due date
// should not grey out the importance you were about to click.
//
// Nothing is applied optimistically. Only the server's answer sets a
// value, which costs a visible fraction of a second and buys a row that
// never shows a state the server rejected -- the failure that makes a
// person stop trusting a write surface.

import { send } from '../shared/protocol'
import type { EntityRow, Failure, TaskPatch } from '../shared/protocol'
import { clear, el, on } from './dom'
import { m } from './i18n'

export interface EditorCallbacks {
  /** The canonical row the server answered with. It replaces what the
   *  panel had rather than being merged into it: the response carries
   *  server-derived fields, and merging is how two copies drift. */
  onRow: (row: EntityRow) => void
  onConflict: () => void
  onFailure: (failure: Failure) => void
  announce: (text: string) => void
}

function ymd(offsetDays: number): string {
  const d = new Date()
  d.setDate(d.getDate() + offsetDays)
  // A bare calendar day, deliberately. The server anchors it to the end
  // of that day in the OWNER's timezone; an instant built from this
  // browser's clock would move somebody else's deadline.
  return d.toISOString().slice(0, 10)
}

export function renderEditor(row: EntityRow, cb: EditorCallbacks): HTMLElement {
  const box = el('div', { class: 'hypha__editor' })
  if (row.version === undefined) {
    // Without a version there is nothing to guard a write with, and a
    // blind write is the one thing this surface will not do.
    box.appendChild(el('p', { class: 'hypha__hint', text: m('loading') }))
    return box
  }
  const version = row.version

  async function write(
    fields: TaskPatch,
    control: HTMLElement,
  ): Promise<void> {
    control.setAttribute('aria-busy', 'true')
    control.setAttribute('disabled', '')
    const res = await send('task/patch', { id: row.id, expectedVersion: version, fields })
    control.removeAttribute('aria-busy')
    control.removeAttribute('disabled')
    if (res.ok) {
      cb.announce(m('saved'))
      cb.onRow(res.data)
      return
    }
    if (res.error.code === 'conflict') {
      // A notice, not an error: nothing the person did was wrong, and
      // the fresh row is about to appear underneath the sentence.
      cb.onConflict()
      return
    }
    cb.onFailure(res.error)
  }

  // --- state -------------------------------------------------------
  const stateRow = el('div', { class: 'hypha__field' })
  stateRow.appendChild(el('span', { class: 'hypha__label', text: m('state') }))
  const stateSelect = el('select', { 'aria-label': m('state') })
  stateRow.appendChild(stateSelect)
  box.appendChild(stateRow)

  void (async () => {
    const res = await send('task/states', { id: row.id })
    if (!res.ok) return
    clear(stateSelect)
    for (const state of res.data) {
      stateSelect.appendChild(
        el('option', { value: state.id, selected: state.id === row.stateId, text: state.name }),
      )
    }
    on(stateSelect, 'change', () => {
      void (async () => {
        stateSelect.setAttribute('disabled', '')
        const moved = await send('task/setState', {
          id: row.id,
          expectedVersion: version,
          stateId: stateSelect.value,
        })
        stateSelect.removeAttribute('disabled')
        if (moved.ok) {
          cb.announce(m('saved'))
          cb.onRow(moved.data)
          return
        }
        if (moved.error.code === 'conflict') return cb.onConflict()
        // A transition the workflow does not allow is a refusal, not a
        // fault: every state is offered because reachability lives in
        // the workflow, and the server is the only thing that knows it.
        cb.onFailure(moved.error)
      })()
    })
  })()

  // --- due ---------------------------------------------------------
  const dueRow = el('div', { class: 'hypha__field' })
  dueRow.appendChild(el('span', { class: 'hypha__label', text: m('due') }))
  for (const [label, value] of [
    [m('today'), ymd(0)],
    [m('tomorrow'), ymd(1)],
    [m('inAWeek'), ymd(7)],
    [m('clearDue'), null],
  ] as [string, string | null][]) {
    const button = el('button', { type: 'button', class: 'hypha__linkbtn' }, [label])
    on(button, 'click', () => void write({ dueDate: value }, button))
    dueRow.appendChild(button)
  }
  box.appendChild(dueRow)

  // --- the two Eisenhower axes ------------------------------------
  // Priority is DERIVED from these on the server and is shown, never
  // set: computing it here would be a second implementation of the rule,
  // and the one that drifts.
  for (const [label, key, held] of [
    [m('importance'), 'importance', undefined],
    [m('urgency'), 'urgency', undefined],
  ] as [string, 'importance' | 'urgency', number | undefined][]) {
    const field = el('div', { class: 'hypha__field', role: 'radiogroup', 'aria-label': label })
    field.appendChild(el('span', { class: 'hypha__label', text: label }))
    for (const n of [1, 2, 3, 4, 5]) {
      const button = el(
        'button',
        {
          type: 'button',
          class: 'hypha__linkbtn',
          role: 'radio',
          'aria-checked': held === n,
        },
        [String(n)],
      )
      on(button, 'click', () => void write({ [key]: n } as TaskPatch, button))
      field.appendChild(button)
    }
    box.appendChild(field)
  }

  if (row.priority != null) {
    box.appendChild(
      el('p', { class: 'hypha__hint', text: `${m('priority')}: P${row.priority}` }),
    )
  }

  // --- title -------------------------------------------------------
  const titleRow = el('div', { class: 'hypha__field' })
  const title = el('input', { type: 'text', value: row.title, 'aria-label': m('captureTitleLabel') })
  const save = el('button', { type: 'button' }, [m('save')])
  on(save, 'click', () => void write({ title: title.value }, save))
  titleRow.append(title, save)
  box.appendChild(titleRow)

  // --- the browser's own move --------------------------------------
  const attach = el('button', { type: 'button' }, [m('attachPage')])
  on(attach, 'click', () => {
    void (async () => {
      attach.setAttribute('disabled', '')
      const res = await send('task/attachPage', { id: row.id, kind: row.kind })
      attach.removeAttribute('disabled')
      if (res.ok) cb.announce(m('attached'))
      else cb.onFailure(res.error)
    })()
  })
  box.appendChild(attach)

  const openIt = el('button', { type: 'button', class: 'hypha__linkbtn' }, [m('open')])
  on(openIt, 'click', () => void chrome.tabs.create({ url: row.route }))
  box.appendChild(openIt)

  return box
}
