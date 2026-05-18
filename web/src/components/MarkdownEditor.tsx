import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api, workspaceHeader } from '../api/client'
import { mentionLink, type MentionKind } from '../lib/mentions'
import { MarkdownView } from './Markdown'
import type { components } from '../api/schema'

type Task = components['schemas']['TaskOut']
type Tag = components['schemas']['TagOut']

// Markdown editor: a textarea (markdown round-trips) with a Write/
// Preview toggle and a reference inserter that writes the @kind:id DSL
// ([label](@task:id)) at the caret. A caret-typeahead is a tracked
// refinement; this is reliable and the references resolve like bitvision.
export function MarkdownEditor({
  value,
  onChange,
  placeholder,
}: {
  value: string
  onChange: (v: string) => void
  placeholder?: string
}) {
  const { t } = useTranslation()
  const ref = useRef<HTMLTextAreaElement | null>(null)
  const [tab, setTab] = useState<'write' | 'preview'>('write')
  const [kind, setKind] = useState<MentionKind>('task')
  const [tasks, setTasks] = useState<Task[]>([])
  const [tags, setTags] = useState<Tag[]>([])
  const [pick, setPick] = useState('')

  useEffect(() => {
    let active = true
    void (async () => {
      const h = workspaceHeader()
      const [tk, tg] = await Promise.all([
        api.GET('/tasks', { params: { header: h } }),
        api.GET('/tags', { params: { header: h } }),
      ])
      if (!active) return
      if (tk.data) setTasks(tk.data)
      if (tg.data) setTags(tg.data)
    })()
    return () => {
      active = false
    }
  }, [])

  function insertRef() {
    if (!pick) return
    let label = pick.slice(0, 8)
    if (kind === 'task') {
      label = tasks.find((x) => x.id === pick)?.title ?? label
    } else {
      label = tags.find((x) => x.id === pick)?.name ?? label
    }
    const snippet = mentionLink(kind, pick, label)
    const el = ref.current
    const at = el ? el.selectionStart : value.length
    const next = value.slice(0, at) + snippet + value.slice(at)
    onChange(next)
    setPick('')
    requestAnimationFrame(() => {
      if (el) {
        el.focus()
        const p = at + snippet.length
        el.setSelectionRange(p, p)
      }
    })
  }

  const opts = kind === 'task' ? tasks : tags

  return (
    <div className="mde">
      <div className="mde__tabs">
        <button
          type="button"
          className={tab === 'write' ? 'btn--sm' : 'btn--ghost btn--sm'}
          onClick={() => setTab('write')}
        >
          {t('md.write')}
        </button>
        <button
          type="button"
          className={tab === 'preview' ? 'btn--sm' : 'btn--ghost btn--sm'}
          onClick={() => setTab('preview')}
        >
          {t('md.preview')}
        </button>
      </div>
      {tab === 'write' ? (
        <>
          <textarea
            ref={ref}
            value={value}
            placeholder={placeholder}
            onChange={(e) => onChange(e.target.value)}
          />
          <div className="mde__ref">
            <select
              value={kind}
              onChange={(e) => {
                setKind(e.target.value as MentionKind)
                setPick('')
              }}
            >
              <option value="task">@task</option>
              <option value="tag">@tag</option>
            </select>
            <select value={pick} onChange={(e) => setPick(e.target.value)}>
              <option value="">{t('md.pick')}</option>
              {opts.map((o) => (
                <option key={o.id} value={o.id}>
                  {'title' in o ? o.title : o.name}
                </option>
              ))}
            </select>
            <button type="button" className="btn--sm" onClick={insertRef}>
              {t('md.insert')}
            </button>
          </div>
        </>
      ) : (
        <div className="mde__preview">
          <MarkdownView text={value} />
        </div>
      )}
    </div>
  )
}
