// The work digest (ADR-0070): what happened on a client's or project's
// tasks in a window, as the server collected it. This module turns the
// local calendar days a person picks into the instants the server wants,
// reads the digest, and renders it as Markdown for pasting into meeting
// notes or into a language model that writes the report.
//
// Nothing here summarises or drops anything: the Markdown carries every
// event the server returned, in its order. Choosing what to tell the client
// is the reader's job.
import type { TFunction } from 'i18next'
import { api, workspaceHeader } from './client'
import type { components } from '../shared'

export type WorkDigest = components['schemas']['WorkDigestOut']
export type TaskDigest = components['schemas']['TaskDigestOut']
export type DigestEvent = components['schemas']['DigestEventOut']

function parseYmd(s: string): [number, number, number] | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s)
  return m ? [Number(m[1]), Number(m[2]) - 1, Number(m[3])] : null
}

/** The half-open instant window `[from 00:00, day after to 00:00)` in the
 *  browser's timezone, as ISO strings with an offset (UTC). The server
 *  refuses a window without one: where a day starts is the reader's call,
 *  and the reader is here. Null for an unparseable or inverted range. */
export function digestWindow(
  from: string,
  to: string,
): { start_from: string; start_to: string } | null {
  const a = parseYmd(from)
  const b = parseYmd(to)
  if (!a || !b) return null
  const start = new Date(a[0], a[1], a[2], 0, 0, 0, 0)
  const end = new Date(b[0], b[1], b[2] + 1, 0, 0, 0, 0)
  if (!(end > start)) return null
  return { start_from: start.toISOString(), start_to: end.toISOString() }
}

export async function fetchWorkDigest(q: {
  start_from: string
  start_to: string
  client_tag_id?: string
  project_tag_id?: string
}): Promise<WorkDigest> {
  const { data, error } = await api.GET('/tasks/digest', {
    params: { header: workspaceHeader(), query: q },
  })
  if (error || !data) throw error ?? new Error('empty digest')
  return data
}

/** Seconds as `1h05`, `0h30`. */
export function hoursLabel(seconds: number): string {
  const m = Math.round(seconds / 60)
  return `${Math.floor(m / 60)}h${String(m % 60).padStart(2, '0')}`
}

function stamp(iso: string): string {
  const d = new Date(iso)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

/** One line of the event, without its timestamp: what happened and who. */
export function eventLine(t: TFunction, e: DigestEvent): string {
  const who = e.actor ? ` @${e.actor}` : ''
  const agent = e.via_agent ? ` (${t('diary.viaAgent')})` : ''
  switch (e.kind) {
    case 'task_created':
      return `${t('diary.ev.task_created')}${who}${agent}`
    case 'state_changed':
      return `${t('diary.ev.state_changed')}: ${e.from_state ?? '?'} → ${e.to_state ?? '?'}${who}${agent}`
    case 'description_edited':
      return `${t('diary.ev.description_edited')}${(e.count ?? 1) > 1 ? ` ×${e.count}` : ''}${who}${agent}`
    case 'comment':
      return `${t(e.reply_to ? 'diary.ev.reply' : 'diary.ev.comment')}${who}${agent}`
    case 'time_entry':
      return `${t('diary.ev.time_entry')} ${
        e.running ? t('diary.running') : hoursLabel(e.duration_seconds ?? 0)
      }${e.billable === false ? ` (${t('diary.notBillable')})` : ''}${who}${agent}`
  }
}

function indent(text: string): string {
  return text
    .split('\n')
    .map((l) => `  ${l}`)
    .join('\n')
}

/** The digest as Markdown: a heading per task, its description quoted, and
 *  every event as a list item with the text that came with it. */
export function digestMarkdown(
  t: TFunction,
  d: WorkDigest,
  heading: { scope: string; from: string; to: string },
): string {
  const out: string[] = [
    `# ${t('diary.title')}: ${heading.scope}, ${heading.from} → ${heading.to}`,
    '',
    `${t('diary.total')}: ${hoursLabel(d.seconds)} (${t('diary.billable')} ${hoursLabel(d.billable_seconds)}), ${t('diary.taskCount', { count: d.tasks.length })}`,
  ]
  if (d.withheld.length > 0) out.push('', `${t('diary.withheld')}: ${d.withheld.join(', ')}`)
  for (const task of d.tasks) {
    const meta = [task.state, task.project_name, hoursLabel(task.seconds)].filter(Boolean)
    out.push('', `## ${task.title} [${task.task_id.slice(0, 8)}] (${meta.join(', ')})`)
    if (task.description?.trim()) {
      out.push('', ...task.description.trim().split('\n').map((l) => `> ${l}`))
    }
    out.push('')
    for (const e of task.events) {
      out.push(`- ${stamp(e.at)} ${eventLine(t, e)}`)
      if (e.text?.trim()) out.push(indent(e.text.trim()))
    }
  }
  return out.join('\n') + '\n'
}
