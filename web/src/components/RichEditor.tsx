import { useEffect } from 'react'
import { Editor as CoreEditor, Extension } from '@tiptap/core'
import { EditorContent, useEditor } from '@tiptap/react'
import StarterKit from '@tiptap/starter-kit'
import Link from '@tiptap/extension-link'
import { Markdown } from 'tiptap-markdown'
import Suggestion, {
  type SuggestionKeyDownProps,
  type SuggestionProps,
} from '@tiptap/suggestion'
import { api, workspaceHeader } from '../api/client'
import { formatMentionHref, type MentionKind } from '../lib/mentions'

// tiptap-markdown augments editor.storage at runtime; type the access.
type MdStorage = { markdown: { getMarkdown: () => string } }
function getMd(ed: CoreEditor): string {
  return (ed.storage as unknown as MdStorage).markdown.getMarkdown()
}

// True WYSIWYG (no preview toggle), markdown round-trip via
// tiptap-markdown, and an inline @ typeahead mirroring bitvision's
// EvidenceMentionExtension: type @ -> search Flow tasks/tags ->
// inserts a [label](@kind:id) link that serializes as the DSL.

type Cand = { kind: MentionKind; id: string; label: string }

async function searchCandidates(query: string): Promise<Cand[]> {
  const h = workspaceHeader()
  const [tk, tg] = await Promise.all([
    api.GET('/tasks', { params: { header: h } }),
    api.GET('/tags', { params: { header: h } }),
  ])
  const q = query.trim().toLowerCase()
  const out: Cand[] = []
  for (const t of tk.data ?? []) {
    if (!q || t.title.toLowerCase().includes(q)) {
      out.push({ kind: 'task', id: t.id, label: t.title })
    }
  }
  for (const g of tg.data ?? []) {
    if (!q || g.name.toLowerCase().includes(q)) {
      out.push({ kind: 'tag', id: g.id, label: g.name })
    }
  }
  return out.slice(0, 8)
}

const MentionExt = Extension.create({
  name: 'flowMention',
  addProseMirrorPlugins() {
    return [
      Suggestion<Cand>({
        editor: this.editor,
        char: '@',
        allowSpaces: false,
        items: ({ query }) => searchCandidates(query),
        command: ({ editor, range, props }) => {
          editor
            .chain()
            .focus()
            .insertContentAt(range, [
              {
                type: 'text',
                text: props.label,
                marks: [
                  {
                    type: 'link',
                    attrs: { href: formatMentionHref(props.kind, props.id) },
                  },
                ],
              },
              { type: 'text', text: ' ' },
            ])
            .run()
        },
        render: () => {
          let box: HTMLDivElement | null = null
          let list: Cand[] = []
          let sel = 0
          let pick: ((c: Cand) => void) | null = null

          const draw = () => {
            const el = box
            if (!el) return
            el.innerHTML = ''
            list.forEach((c, i) => {
              const row = document.createElement('div')
              row.className =
                'mention-pop__row' + (i === sel ? ' mention-pop__row--sel' : '')
              row.textContent = `@${c.kind}: ${c.label}`
              row.addEventListener('mousedown', (e) => {
                e.preventDefault()
                pick?.(c)
              })
              el.append(row)
            })
            if (list.length === 0) {
              el.textContent = '...'
            }
          }
          const place = (rect: DOMRect | null | undefined) => {
            if (!box || !rect) return
            box.style.left = `${rect.left}px`
            box.style.top = `${rect.bottom + 4}px`
          }

          return {
            onStart: (p: SuggestionProps<Cand>) => {
              box = document.createElement('div')
              box.className = 'mention-pop'
              document.body.append(box)
              list = p.items
              sel = 0
              pick = (c) => p.command(c)
              place(p.clientRect?.())
              draw()
            },
            onUpdate: (p: SuggestionProps<Cand>) => {
              list = p.items
              sel = 0
              pick = (c) => p.command(c)
              place(p.clientRect?.())
              draw()
            },
            onKeyDown: (p: SuggestionKeyDownProps) => {
              if (p.event.key === 'ArrowDown') {
                sel = list.length ? (sel + 1) % list.length : 0
                draw()
                return true
              }
              if (p.event.key === 'ArrowUp') {
                sel = list.length ? (sel - 1 + list.length) % list.length : 0
                draw()
                return true
              }
              if (p.event.key === 'Enter') {
                if (list[sel] && pick) pick(list[sel])
                return true
              }
              if (p.event.key === 'Escape') return true
              return false
            },
            onExit: () => {
              box?.remove()
              box = null
            },
          }
        },
      }),
    ]
  },
})

export function RichEditor({
  value,
  onChange,
  placeholder,
}: {
  value: string
  onChange: (v: string) => void
  placeholder?: string
}) {
  const editor = useEditor({
    extensions: [
      StarterKit,
      Link.configure({ openOnClick: false, autolink: false }),
      Markdown.configure({ html: false }),
      MentionExt,
    ],
    content: value,
    onUpdate: ({ editor }: { editor: CoreEditor }) => {
      onChange(getMd(editor))
    },
  })

  // Reflect external value changes (e.g. loaded task) without looping.
  useEffect(() => {
    if (!editor) return
    const current = getMd(editor)
    if (value !== current) {
      editor.commands.setContent(value, { emitUpdate: false })
    }
  }, [value, editor])

  return (
    <div className="rte" data-placeholder={placeholder}>
      <EditorContent editor={editor} />
    </div>
  )
}
