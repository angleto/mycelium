// Getting the page in front of you into Mycelium.
//
// Two phases, and the confirmation tells the truth about both: the entity
// is created, then each attachment is uploaded on its own request. If one
// upload fails the entity still exists, so "created, one file did not
// attach" is what to say -- a blanket failure sends somebody hunting for
// a task that is already there, and they make a second one.
//
// The file input lives in the SIDE PANEL only. Opening an OS file dialog
// dismisses a Chrome popup and takes the draft with it, so offering it
// there would be offering a control that destroys your work.

import { send } from '../shared/protocol'
import type { CaptureDraft, Failure, Host, PageContext } from './types'
import { el, on } from './dom'
import { m } from './i18n'

export interface CaptureCallbacks {
  onDone: (message: string) => void
  onFailure: (failure: Failure) => void
  onClose: () => void
}

function firstLine(text: string, cap: number): string {
  const line = text.split('\n').find((l) => l.trim().length > 0) ?? ''
  if (line.length <= cap) return line.trim()
  const cut = line.slice(0, cap)
  const boundary = cut.lastIndexOf(' ')
  return (boundary > cap / 2 ? cut.slice(0, boundary) : cut).trim()
}

export function renderCapture(
  page: PageContext,
  host: Host,
  cb: CaptureCallbacks,
): HTMLElement {
  const box = el('section', { class: 'hypha__capture' })
  box.appendChild(el('h2', { text: m('captureTitle') }))

  let kind: 'task' | 'note' = 'task'
  const attachments: CaptureDraft['attachments'] = []
  // Minted once, for this sheet. Pressing Create twice after a timeout
  // presents the same key, so the server replays its first answer
  // instead of filing a second task.
  const idempotencyKey = crypto.randomUUID()

  const kindRow = el('div', { class: 'hypha__field', role: 'radiogroup', 'aria-label': m('captureTitle') })
  const asTask = el('button', { type: 'button', role: 'radio', 'aria-checked': true }, [m('captureAsTask')])
  const asNote = el('button', { type: 'button', class: 'hypha__linkbtn', role: 'radio', 'aria-checked': false }, [
    m('captureAsNote'),
  ])
  kindRow.append(asTask, asNote)
  box.appendChild(kindRow)

  const titleInput = el('input', {
    type: 'text',
    'aria-label': m('captureTitleLabel'),
    // With a selection the title is its first line; without, the tab's
    // own title. Capped at what the server accepts, cut on a word.
    value: page.selection ? firstLine(page.selection, 120) : (page.title ?? '').slice(0, 300),
  })
  box.append(el('span', { class: 'hypha__label', text: m('captureTitleLabel') }), titleInput)

  // The URL goes in the BODY, as the last line, always in the same shape.
  // There is no url field on a task, and the app renders markdown in a
  // description, so the link is clickable where it lands. Never in the
  // title, which is what a person scans.
  const source = page.url ? `[${(page.title ?? page.url).replace(/[[\]]/g, '')}](${page.url})` : ''
  const quoted = page.selection
    ? page.selection
        .split('\n')
        .map((line) => `> ${line}`)
        .join('\n')
    : ''
  const bodyInput = el('textarea', { rows: 6, 'aria-label': m('captureBodyLabel') })
  bodyInput.value = [quoted, source].filter(Boolean).join('\n\n')
  box.append(el('span', { class: 'hypha__label', text: m('captureBodyLabel') }), bodyInput)

  if (page.selectionBlocked) {
    // A notice, not an error: capture never blocks, and only the
    // selection was lost.
    box.appendChild(el('p', { class: 'hypha__hint', role: 'status', text: m('captureSelectionBlocked') }))
  }

  const attachRow = el('div', { class: 'hypha__field' })
  const shot = el('button', { type: 'button', class: 'hypha__linkbtn' }, [m('captureScreenshot')])
  const attached = el('span', { class: 'hypha__hint' })
  on(shot, 'click', () => {
    void (async () => {
      const res = await send('capture/screenshot')
      if (!res.ok) {
        // activeTab is granted for the tab the extension was invoked
        // from and does not follow a tab switch. Saying which tab beats
        // a bare refusal.
        cb.onFailure({ ...res.error, message: m('screenshotWrongTab') })
        return
      }
      attachments.push(res.data)
      attached.textContent = attachments.map((a) => a.name).join(', ')
    })()
  })
  attachRow.append(shot)

  if (host === 'sidepanel') {
    const file = el('input', { type: 'file', 'aria-label': m('captureFile') })
    on(file, 'change', () => {
      const chosen = file.files?.[0]
      if (!chosen) return
      const reader = new FileReader()
      reader.onload = () => {
        attachments.push({
          name: chosen.name,
          mime: chosen.type || 'application/octet-stream',
          dataUrl: String(reader.result),
        })
        attached.textContent = attachments.map((a) => a.name).join(', ')
      }
      reader.readAsDataURL(chosen)
    })
    attachRow.append(file)
  } else {
    attachRow.appendChild(el('span', { class: 'hypha__hint', text: m('captureFileSidePanelOnly') }))
  }
  attachRow.appendChild(attached)
  box.appendChild(attachRow)

  const projectSelect = el('select', { 'aria-label': m('captureProject') })
  projectSelect.appendChild(el('option', { value: '', text: m('captureNoProject') }))
  box.append(el('span', { class: 'hypha__label', text: m('captureProject') }), projectSelect)
  void (async () => {
    const res = await send('scope/projects', { q: '' })
    if (!res.ok) return
    for (const project of res.data) {
      projectSelect.appendChild(el('option', { value: project.id, text: project.name }))
    }
  })()

  async function create(andOpen: boolean, control: HTMLElement): Promise<void> {
    control.setAttribute('disabled', '')
    const res = await send('capture/create', {
      kind,
      idempotencyKey,
      title: titleInput.value,
      body: bodyInput.value,
      projectTagId: projectSelect.value || null,
      clientTagId: null,
      attachments,
    })
    control.removeAttribute('disabled')
    if (!res.ok) return cb.onFailure(res.error)
    cb.onDone(
      res.data.attachmentsFailed.length
        ? m('captureAttachmentsFailed', res.data.attachmentsFailed.join(', '))
        : m('captureDone', res.data.code),
    )
    if (andOpen) void chrome.tabs.create({ url: res.data.route })
    else cb.onClose()
  }

  const actions = el('div', { class: 'hypha__field' })
  const createButton = el('button', { type: 'button' }, [m('captureCreate')])
  const createOpen = el('button', { type: 'button', class: 'hypha__linkbtn' }, [
    m('captureCreateAndOpen'),
  ])
  const cancel = el('button', { type: 'button', class: 'hypha__linkbtn' }, [m('cancel')])
  on(createButton, 'click', () => void create(false, createButton))
  on(createOpen, 'click', () => void create(true, createOpen))
  on(cancel, 'click', cb.onClose)
  actions.append(createButton, createOpen, cancel)
  box.appendChild(actions)

  function setKind(next: 'task' | 'note'): void {
    kind = next
    asTask.className = next === 'task' ? '' : 'hypha__linkbtn'
    asNote.className = next === 'note' ? '' : 'hypha__linkbtn'
    asTask.setAttribute('aria-checked', String(next === 'task'))
    asNote.setAttribute('aria-checked', String(next === 'note'))
  }
  on(asTask, 'click', () => setKind('task'))
  on(asNote, 'click', () => setKind('note'))

  titleInput.focus()
  return box
}
