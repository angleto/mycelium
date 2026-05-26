import { useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Editor as CoreEditor, Extension, Node } from '@tiptap/core'
import {
  EditorContent,
  NodeViewWrapper,
  ReactNodeViewRenderer,
  useEditor,
  type NodeViewProps,
} from '@tiptap/react'
import StarterKit from '@tiptap/starter-kit'
import Link from '@tiptap/extension-link'
import TaskList from '@tiptap/extension-task-list'
import TaskItem from '@tiptap/extension-task-item'
import { Table, TableCell, TableHeader, TableRow } from '@tiptap/extension-table'
import { Markdown } from 'tiptap-markdown'
import Suggestion, {
  type SuggestionKeyDownProps,
  type SuggestionProps,
} from '@tiptap/suggestion'
import { InlineMath, BlockMath } from './MarkdownMath'
import { api, workspaceHeader } from '../api/client'
import { formatMentionHref, type MentionKind } from '../lib/mentions'
import { useAuthBlobUrl } from '../lib/useAuthBlobUrl'
import {
  ACCEPTED_IMAGE_MIME,
  isAcceptedImage,
  uploadImage,
  type ImageUploadParent,
} from '../lib/imageUpload'

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
  const [tk, tg, nt] = await Promise.all([
    api.GET('/tasks', { params: { header: h } }),
    api.GET('/tags', { params: { header: h } }),
    api.GET('/notes', { params: { header: h } }),
  ])
  const q = query.trim().toLowerCase()
  const out: Cand[] = []
  for (const t of tk.data ?? []) {
    if (!q || t.title.toLowerCase().includes(q)) {
      out.push({ kind: 'task', id: t.id, label: t.title })
    }
  }
  for (const n of nt.data ?? []) {
    const label = n.title ?? n.kind
    if (!q || label.toLowerCase().includes(q)) {
      out.push({ kind: 'note', id: n.id, label })
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
              row.id = `mention-opt-${i}`
              row.setAttribute('role', 'option')
              row.setAttribute('aria-selected', i === sel ? 'true' : 'false')
              row.textContent = `@${c.kind}: ${c.label}`
              row.addEventListener('mousedown', (e) => {
                e.preventDefault()
                pick?.(c)
              })
              el.append(row)
            })
            // Point the listbox at the active option for screen readers.
            if (list.length > 0) el.setAttribute('aria-activedescendant', `mention-opt-${sel}`)
            else {
              el.removeAttribute('aria-activedescendant')
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
              box.setAttribute('role', 'listbox')
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

// Live preview of an embedded image inside the editor. The src in the
// markdown is "/attachments/<id>/download" (bearer-auth route); the
// node view auth-fetches it and shows the resulting object URL, with
// the same lifecycle behaviour as the read-side <img>.
function ImageNodeView({ node }: NodeViewProps) {
  const src = typeof node.attrs.src === 'string' ? node.attrs.src : ''
  const alt = typeof node.attrs.alt === 'string' ? node.attrs.alt : ''
  const title = typeof node.attrs.title === 'string' ? node.attrs.title : undefined
  const resolved = useAuthBlobUrl(src)
  if (!resolved) {
    return (
      <NodeViewWrapper as="span" className="md-img md-img--loading" />
    )
  }
  return (
    <NodeViewWrapper as="span" className="md-img-wrap">
      <img src={resolved} alt={alt} title={title} className="md-img" />
    </NodeViewWrapper>
  )
}

// Inline image node. Same shape prosemirror-markdown / tiptap-markdown
// expect (``name: 'image'``, attrs ``src/alt/title``), so the round-trip
// to `![alt](src "title")` markdown is automatic. ``atom`` keeps the
// node selectable-as-a-whole; the node view handles preview.
const ImageExt = Node.create({
  name: 'image',
  inline: true,
  group: 'inline',
  draggable: true,
  selectable: true,
  atom: true,
  addAttributes() {
    return {
      src: { default: '' },
      alt: { default: null },
      title: { default: null },
    }
  },
  parseHTML() {
    return [{ tag: 'img[src]' }]
  },
  renderHTML({ HTMLAttributes }) {
    return ['img', HTMLAttributes]
  },
  addNodeView() {
    return ReactNodeViewRenderer(ImageNodeView)
  },
})

export function RichEditor({
  value,
  onChange,
  placeholder,
  large,
  imageUploadParent,
}: {
  value: string
  onChange: (v: string) => void
  placeholder?: string
  large?: boolean
  imageUploadParent?: ImageUploadParent
}) {
  const { t } = useTranslation()
  // Drop to a plain markdown textarea (paste long blocks, fix a bad
  // round-trip). Both modes read/write the same `value` markdown
  // string (bitvision EvidenceEditor pattern).
  const [rawMode, setRawMode] = useState(false)
  const [uploadErr, setUploadErr] = useState<string | null>(null)
  const [uploading, setUploading] = useState(false)
  const imgInput = useRef<HTMLInputElement>(null)
  const rawRef = useRef<HTMLTextAreaElement>(null)

  // Keep the latest parent in a ref so the editorProps handlers below
  // (created once when the editor is built) see the live value even if
  // the prop changes mid-session (e.g. a freshly created note id
  // arriving from the save call).
  const parentRef = useRef<ImageUploadParent | undefined>(imageUploadParent)
  useEffect(() => {
    parentRef.current = imageUploadParent
  }, [imageUploadParent])

  // Insert markdown image syntax at the raw-mode caret (or append on
  // the end if not focused). The WYSIWYG branch goes through the
  // editor's insertContent below.
  const insertRawImage = (url: string, alt: string) => {
    const md = `![${alt}](${url})`
    const ta = rawRef.current
    if (!ta) {
      onChange(value + (value.endsWith('\n') || !value ? '' : '\n') + md + '\n')
      return
    }
    const start = ta.selectionStart ?? value.length
    const end = ta.selectionEnd ?? value.length
    const next = value.slice(0, start) + md + value.slice(end)
    onChange(next)
    requestAnimationFrame(() => {
      ta.focus()
      const pos = start + md.length
      ta.setSelectionRange(pos, pos)
    })
  }

  const doUpload = async (file: File): Promise<void> => {
    const parent = parentRef.current
    if (!parent) {
      setUploadErr(t('editor.imageNeedsSave'))
      return
    }
    if (!isAcceptedImage(file)) return
    setUploadErr(null)
    setUploading(true)
    try {
      const up = await uploadImage(parent, file)
      if (rawMode) {
        insertRawImage(up.url, up.filename)
      } else if (editorRef.current) {
        editorRef.current
          .chain()
          .focus()
          .insertContent({
            type: 'image',
            attrs: { src: up.url, alt: up.filename, title: null },
          })
          .run()
      }
    } catch (e) {
      setUploadErr(e instanceof Error ? e.message : String(e))
    } finally {
      setUploading(false)
    }
  }

  // Stable ref to the editor for paste/drop handlers (which are bound
  // at editor-build time and must not capture a stale editor instance).
  const editorRef = useRef<CoreEditor | null>(null)

  const editorProps = useMemo(
    () => ({
      handlePaste: (_view: unknown, event: ClipboardEvent) => {
        if (!parentRef.current) return false
        const items = event.clipboardData?.items
        if (!items) return false
        for (let i = 0; i < items.length; i += 1) {
          const it = items[i]
          if (it.kind === 'file') {
            const f = it.getAsFile()
            if (f && isAcceptedImage(f)) {
              event.preventDefault()
              void doUpload(f)
              return true
            }
          }
        }
        return false
      },
      handleDrop: (
        _view: unknown,
        event: DragEvent,
        _slice: unknown,
        moved: boolean,
      ) => {
        if (moved) return false
        if (!parentRef.current) return false
        const files = event.dataTransfer?.files
        if (!files || files.length === 0) return false
        let any = false
        for (let i = 0; i < files.length; i += 1) {
          const f = files[i]
          if (isAcceptedImage(f)) {
            void doUpload(f)
            any = true
          }
        }
        if (any) event.preventDefault()
        return any
      },
    }),
    // doUpload closes over `rawMode` etc. but parentRef/editorRef are
    // refs; keeping handlers stable for the editor's lifetime is fine
    // because they read everything they need through refs.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  )

  const editor = useEditor({
    extensions: [
      // StarterKit v3 bundles its own Link mark; disable it so our
      // single configured Link wins. With both registered the
      // StarterKit one (openOnClick: true) took over and clicking a
      // mention did window.open('@note:uuid') -> broken tab instead
      // of letting the app-side interceptor route it (the reason a
      // note opened from a converted task was unreachable).
      StarterKit.configure({ link: false }),
      Link.configure({
        openOnClick: false,
        autolink: false,
        // Accept the @kind:uuid mention DSL hrefs in addition to
        // regular links so [label](@note:uuid) survives the markdown
        // round-trip as a clickable link (bitvision pattern).
        validate: (url: string) => {
          if (!url) return false
          if (/^@(?:task|note|tag):/.test(url)) return true
          return /^(https?:|mailto:|tel:)/i.test(url)
        },
      }),
      // GitHub-flavored task lists: round-trip via tiptap-markdown's
      // built-in task_list / task_item serializers (`- [ ]` / `- [x]`).
      TaskList,
      TaskItem.configure({ nested: true }),
      // GFM tables. tiptap-markdown carries a table serializer keyed on
      // these node names, so pasting/typing a `| a | b |` table in raw
      // mode round-trips to a real table in WYSIWYG and back.
      Table.configure({ resizable: false }),
      TableRow,
      TableHeader,
      TableCell,
      Markdown.configure({ html: false }),
      // LaTeX math (``$inline$`` and ``$$block$$``): KaTeX NodeView in
      // the editor + markdown-it round-trip, so what /garden's
      // MarkdownView renders read-side is what the author saw write-side.
      InlineMath,
      BlockMath,
      // Embedded images via /attachments uploads. Round-trips to the
      // standard `![alt](src)` markdown; the node view auth-fetches the
      // bytes for live preview.
      ImageExt,
      MentionExt,
    ],
    content: value,
    editorProps,
    onUpdate: ({ editor }: { editor: CoreEditor }) => {
      onChange(getMd(editor))
    },
  })

  useEffect(() => {
    editorRef.current = editor
  }, [editor])

  // Reflect external value changes (loaded task, or raw-mode edits)
  // without looping. This also keeps the hidden editor synced while in
  // raw mode, so flipping back to WYSIWYG already shows the latest.
  useEffect(() => {
    if (!editor) return
    const current = getMd(editor)
    if (value !== current) {
      editor.commands.setContent(value, { emitUpdate: false })
    }
  }, [value, editor])

  const fmt = !rawMode && editor != null
  const tb = (
    label: string,
    titleKey: string,
    run: () => void,
    activeName?: string,
    activeAttrs?: Record<string, unknown>,
  ) => (
    <button
      type="button"
      className={
        'btn--ghost btn--sm rte__fmt' +
        (activeName && editor?.isActive(activeName, activeAttrs)
          ? ' rte__fmt--on'
          : '')
      }
      title={t(titleKey)}
      disabled={!fmt}
      onClick={run}
    >
      {label}
    </button>
  )

  // Link insert/edit: prompt for a URL (empty clears the link). The
  // editor's own Link.validate rejects unsafe schemes.
  const setLink = () => {
    if (!editor) return
    const prev = (editor.getAttributes('link').href as string | undefined) ?? ''
    const url = window.prompt(t('editor.linkPrompt'), prev)
    if (url === null) return
    if (url === '') {
      editor.chain().focus().extendMarkRange('link').unsetLink().run()
      return
    }
    editor.chain().focus().extendMarkRange('link').setLink({ href: url }).run()
  }

  // Image button: opens the hidden file input. Disabled when no parent
  // is available (e.g. note-create form before save) — drop/paste are
  // also gated by parentRef inside the handlers.
  const imageDisabled =
    (!fmt && !rawMode) || uploading || !imageUploadParent
  const imageTitle = imageUploadParent
    ? t('editor.image')
    : t('editor.imageNeedsSave')

  return (
    <div
      className={'rte' + (large ? ' rte--lg' : '')}
      data-placeholder={placeholder}
    >
      <div className="rte__bar">
        <span className="rte__tools">
          {tb('B', 'editor.bold', () =>
            editor?.chain().focus().toggleBold().run(), 'bold')}
          {tb('I', 'editor.italic', () =>
            editor?.chain().focus().toggleItalic().run(), 'italic')}
          {tb('S', 'editor.strike', () =>
            editor?.chain().focus().toggleStrike().run(), 'strike')}
          {tb('H1', 'editor.h1', () =>
            editor?.chain().focus().toggleHeading({ level: 1 }).run(),
            'heading', { level: 1 })}
          {tb('H2', 'editor.h2', () =>
            editor?.chain().focus().toggleHeading({ level: 2 }).run(),
            'heading', { level: 2 })}
          {tb('H3', 'editor.h3', () =>
            editor?.chain().focus().toggleHeading({ level: 3 }).run(),
            'heading', { level: 3 })}
          {tb('•', 'editor.bullet', () =>
            editor?.chain().focus().toggleBulletList().run(), 'bulletList')}
          {tb('1.', 'editor.ordered', () =>
            editor?.chain().focus().toggleOrderedList().run(), 'orderedList')}
          {tb('☑', 'editor.checklist', () =>
            editor?.chain().focus().toggleTaskList().run(), 'taskList')}
          {tb('❝', 'editor.quote', () =>
            editor?.chain().focus().toggleBlockquote().run(), 'blockquote')}
          {tb('</>', 'editor.code', () =>
            editor?.chain().focus().toggleCode().run(), 'code')}
          {tb('{ }', 'editor.codeBlock', () =>
            editor?.chain().focus().toggleCodeBlock().run(), 'codeBlock')}
          {tb('🔗', 'editor.link', setLink, 'link')}
          <button
            type="button"
            className="btn--ghost btn--sm rte__fmt"
            title={imageTitle}
            disabled={imageDisabled}
            onClick={() => imgInput.current?.click()}
          >
            {uploading ? '⏳' : '🖼'}
          </button>
          {tb('―', 'editor.hr', () =>
            editor?.chain().focus().setHorizontalRule().run())}
          <button
            type="button"
            className="btn--ghost btn--sm rte__fmt"
            title={t('editor.table')}
            disabled={!fmt}
            onClick={() =>
              editor
                ?.chain()
                .focus()
                .insertTable({ rows: 3, cols: 3, withHeaderRow: true })
                .run()
            }
          >
            ▦
          </button>
          {fmt && editor?.isActive('table') && (
            <>
              {tb('+row', 'editor.tableRow', () =>
                editor?.chain().focus().addRowAfter().run())}
              {tb('+col', 'editor.tableCol', () =>
                editor?.chain().focus().addColumnAfter().run())}
              {tb('✕tbl', 'editor.tableDel', () =>
                editor?.chain().focus().deleteTable().run())}
            </>
          )}
          {tb('↶', 'editor.undo', () =>
            editor?.chain().focus().undo().run())}
          {tb('↷', 'editor.redo', () =>
            editor?.chain().focus().redo().run())}
        </span>
        <button
          type="button"
          className="btn--ghost btn--sm"
          onClick={() => setRawMode((v) => !v)}
        >
          {rawMode ? t('editor.toWysiwyg') : t('editor.toRaw')}
        </button>
      </div>
      <input
        ref={imgInput}
        type="file"
        accept={ACCEPTED_IMAGE_MIME.join(',')}
        hidden
        onChange={(e) => {
          const f = e.target.files?.[0]
          e.target.value = ''
          if (f) void doUpload(f)
        }}
      />
      {uploadErr && <p className="err rte__err">{uploadErr}</p>}
      {rawMode ? (
        <textarea
          ref={rawRef}
          className="rte__raw"
          value={value}
          placeholder={placeholder}
          onChange={(e) => onChange(e.target.value)}
          onPaste={(e) => {
            if (!parentRef.current) return
            const items = e.clipboardData?.items
            if (!items) return
            for (let i = 0; i < items.length; i += 1) {
              const it = items[i]
              if (it.kind === 'file') {
                const f = it.getAsFile()
                if (f && isAcceptedImage(f)) {
                  e.preventDefault()
                  void doUpload(f)
                  return
                }
              }
            }
          }}
          onDrop={(e) => {
            if (!parentRef.current) return
            const files = e.dataTransfer?.files
            if (!files || files.length === 0) return
            let any = false
            for (let i = 0; i < files.length; i += 1) {
              const f = files[i]
              if (isAcceptedImage(f)) {
                void doUpload(f)
                any = true
              }
            }
            if (any) e.preventDefault()
          }}
        />
      ) : (
        <EditorContent editor={editor} />
      )}
    </div>
  )
}
