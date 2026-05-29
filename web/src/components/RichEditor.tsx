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
import {
  AnnotationDecorations,
  annotationKey,
  type AnnotationAnchor,
} from '../lib/annotationDecorations'
import { EntityPrefix } from '../lib/entityPrefixExtension'
import { api, authFetch, workspaceHeader } from '../api/client'
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

// Strip characters that are unsafe or awkward in filenames across
// macOS / Windows / Linux. Falls back to ``untitled`` for an empty
// title so the download attribute always carries a usable name.
function slugifyFilename(name: string | undefined): string {
  const s = (name ?? '').trim()
  if (!s) return 'untitled'
  const FORBIDDEN = /[\\/:*?"<>|]/g
  const cleaned = s
    .replace(FORBIDDEN, '-')
    .replace(/\s+/g, ' ')
    .trim()
  return (cleaned || 'untitled').slice(0, 120)
}

function downloadText(filename: string, mime: string, content: string): void {
  const blob = new Blob([content], { type: mime })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.rel = 'noopener'
  document.body.append(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 0)
}

// Bearer-auth attachment routes 401 from inside a print iframe (no
// Authorization header propagates). Fetch each ``/attachments/...``
// image with the SPA's authFetch and rewrite the src to a data URL
// so the printable copy is fully self-contained.
async function inlineAuthImages(html: string): Promise<string> {
  const tmp = document.createElement('div')
  tmp.innerHTML = html
  const imgs = Array.from(tmp.querySelectorAll('img'))
  await Promise.all(
    imgs.map(async (img) => {
      const src = img.getAttribute('src')
      if (!src || !src.startsWith('/')) return
      try {
        const res = await authFetch(src)
        if (!res.ok) return
        const blob = await res.blob()
        const dataUrl = await new Promise<string>((resolve, reject) => {
          const r = new FileReader()
          r.onload = () => resolve(String(r.result))
          r.onerror = () => reject(r.error)
          r.readAsDataURL(blob)
        })
        img.setAttribute('src', dataUrl)
      } catch {
        // Leave the original src; the print view will show a broken
        // image rather than blocking the export.
      }
    }),
  )
  return tmp.innerHTML
}

// Server-side WeasyPrint export. The SPA inlines authenticated
// attachment images so the backend never has to deauthorise itself
// against /attachments/<id>; the rest (typography, page rules, KaTeX
// fonts) is handled by the bundled print.css.
async function exportPdfViaServer(
  title: string,
  bodyHtml: string,
): Promise<void> {
  const inlined = await inlineAuthImages(bodyHtml)
  const res = await authFetch('/export/pdf', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title, html: inlined }),
  })
  if (!res.ok) {
    let detail = ''
    try {
      const body = (await res.json()) as { detail?: unknown }
      if (typeof body.detail === 'string') detail = body.detail
    } catch {
      detail = await res.text().catch(() => '')
    }
    throw new Error(detail || `HTTP ${res.status}`)
  }
  const blob = await res.blob()
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filenameFromContentDisposition(
    res.headers.get('content-disposition'),
  ) || `${slugifyFilename(title)}.pdf`
  a.rel = 'noopener'
  document.body.append(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 0)
}

function filenameFromContentDisposition(header: string | null): string {
  if (!header) return ''
  const m = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(header)
  return m ? decodeURIComponent(m[1]) : ''
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
  filename,
  annotations,
  onCommentSelection,
  onSuggestSelection,
}: {
  value: string
  onChange: (v: string) => void
  placeholder?: string
  large?: boolean
  imageUploadParent?: ImageUploadParent
  // Base name (no extension) used by the Download / Export-PDF
  // toolbar actions. Pass the surrounding task or note title; the
  // editor slugifies it for filesystem safety. Defaults to
  // ``untitled`` when missing.
  filename?: string
  // Inline annotation anchors (comments + suggestions) rendered as
  // decorations over the live prose. Purely presentational.
  annotations?: AnnotationAnchor[]
  // Selection-driven authoring: when provided, the toolbar shows
  // "Comment" / "Suggest edit" actions that hand the current selection
  // (text + W3C prefix/suffix context) to the host to prefill the
  // annotation form.
  onCommentSelection?: (sel: { text: string; prefix: string; suffix: string }) => void
  onSuggestSelection?: (sel: { text: string; prefix: string; suffix: string }) => void
}) {
  const { t } = useTranslation()
  // Drop to a plain markdown textarea (paste long blocks, fix a bad
  // round-trip). Both modes read/write the same `value` markdown
  // string (bitvision EvidenceEditor pattern).
  const [rawMode, setRawMode] = useState(false)
  const [uploadErr, setUploadErr] = useState<string | null>(null)
  const [uploading, setUploading] = useState(false)
  const [pdfBusy, setPdfBusy] = useState(false)
  const [pdfErr, setPdfErr] = useState<string | null>(null)
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

  // Markdown the editor currently holds. Updated on every emit
  // (onUpdate) and after an external setContent, so the value-sync
  // effect can tell an *external* value change (a different part/note
  // loaded, a raw-mode edit, a conflict reload) from the editor's own
  // content echoing back through the parent. Re-running setContent on
  // our own echo is what rebuilt the document on every keystroke and
  // moved the caret on every autosave.
  const lastEmittedRef = useRef(value)

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
      // Clickable UUID-prefix chips for backticked codes (ADR-0038
      // convention) read inside the editor. Decoration-only; routing
      // is the global AppShell interceptor (data-entity-prefix).
      EntityPrefix,
      // Inline comment/suggestion decorations (presentational; updated
      // via a meta transaction in the effect below).
      AnnotationDecorations.configure({ anchors: annotations ?? [] }),
    ],
    content: value,
    editorProps,
    onUpdate: ({ editor }: { editor: CoreEditor }) => {
      const md = getMd(editor)
      lastEmittedRef.current = md
      onChange(md)
    },
  })

  useEffect(() => {
    editorRef.current = editor
  }, [editor])

  // Push the current annotation anchors into the decoration plugin
  // whenever they change. This is a meta-only transaction (no doc
  // change), so it never affects the markdown round-trip or the caret.
  useEffect(() => {
    if (!editor) return
    editor.view.dispatch(editor.state.tr.setMeta(annotationKey, annotations ?? []))
  }, [annotations, editor])

  // Reflect *external* value changes (a different part/note loaded, a
  // raw-mode edit, a conflict reload) onto the editor, while leaving
  // the editor untouched when the incoming ``value`` is just its own
  // content echoing back through the parent.
  //
  // The previous implementation compared ``value`` against the last
  // prop it had seen, but it still ran ``setContent`` on every
  // keystroke (each keystroke emits a new markdown string, so the prop
  // genuinely changed) — rebuilding the document every time. Because
  // the Markdown↔ProseMirror round-trip is not idempotent at the
  // character level (trailing newlines, escape ordering), and because
  // NotePartsEditor used to feed the server's re-normalised body back
  // into ``value`` on save, that rebuild moved the caret to a wrong
  // position on autosave / manual save.
  //
  // The fix: skip the rebuild whenever ``value`` equals the markdown
  // the editor itself last produced (``lastEmittedRef``). Only a
  // genuinely external value triggers ``setContent``, and there we
  // preserve and clamp the prior selection so the caret stays put.
  useEffect(() => {
    if (!editor) return
    if (value === lastEmittedRef.current) return
    lastEmittedRef.current = value
    const { from, to } = editor.state.selection
    editor.commands.setContent(value, { emitUpdate: false })
    const max = editor.state.doc.content.size
    editor.commands.setTextSelection({
      from: Math.min(from, max),
      to: Math.min(to, max),
    })
  }, [value, editor])

  const fmt = !rawMode && editor != null

  const hasSelection = editor != null && !editor.state.selection.empty
  // Current selection as text + W3C-style prefix/suffix (the chars
  // around it within the same block) for robust annotation anchoring.
  const selectionContext = (): { text: string; prefix: string; suffix: string } | null => {
    if (!editor) return null
    const { from, to } = editor.state.selection
    const doc = editor.state.doc
    return {
      text: doc.textBetween(from, to, ' '),
      prefix: doc.textBetween(doc.resolve(from).start(), from, ' ').slice(-24),
      suffix: doc.textBetween(to, doc.resolve(to).end(), ' ').slice(0, 24),
    }
  }
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
        <span className="rte__actions">
          {onCommentSelection && (
            <button
              type="button"
              className="btn--ghost btn--sm"
              disabled={!fmt}
              title={t('annotations.comment', { defaultValue: 'Comment' })}
              onClick={() => {
                const c = selectionContext()
                if (c) onCommentSelection(c)
              }}
            >
              💬
            </button>
          )}
          {onSuggestSelection && (
            <button
              type="button"
              className="btn--ghost btn--sm"
              disabled={!fmt || !hasSelection}
              title={t('annotations.suggestToggle', { defaultValue: 'Suggest an edit' })}
              onClick={() => {
                const c = selectionContext()
                if (c && c.text) onSuggestSelection(c)
              }}
            >
              ✎
            </button>
          )}
          <button
            type="button"
            className="btn--ghost btn--sm"
            title={t('editor.downloadMd', {
              defaultValue: 'Scarica markdown (.md)',
            })}
            onClick={() => {
              const slug = slugifyFilename(filename)
              downloadText(`${slug}.md`, 'text/markdown;charset=utf-8', value)
            }}
          >
            {t('editor.downloadMdShort', { defaultValue: '.md' })}
          </button>
          <button
            type="button"
            className="btn--ghost btn--sm"
            disabled={pdfBusy || !editor}
            title={t('editor.exportPdf', {
              defaultValue: 'Esporta PDF',
            })}
            onClick={() => {
              if (!editor) return
              const html = editor.getHTML()
              const slug = slugifyFilename(filename)
              setPdfBusy(true)
              setPdfErr(null)
              void exportPdfViaServer(slug, html)
                .catch((e: unknown) =>
                  setPdfErr(e instanceof Error ? e.message : String(e)),
                )
                .finally(() => setPdfBusy(false))
            }}
          >
            {pdfBusy
              ? '⏳'
              : t('editor.exportPdfShort', { defaultValue: '.pdf' })}
          </button>
          <button
            type="button"
            className="btn--ghost btn--sm"
            onClick={() => setRawMode((v) => !v)}
          >
            {rawMode ? t('editor.toWysiwyg') : t('editor.toRaw')}
          </button>
        </span>
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
      {pdfErr && (
        <p className="err rte__err">
          {t('editor.exportPdfErr', { defaultValue: 'Export PDF: ' })}
          {pdfErr}
        </p>
      )}
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
