import { useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState, type Ref } from 'react'
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
import { CodeBlockMermaid } from './MermaidCodeBlock'
import Suggestion, {
  type SuggestionKeyDownProps,
  type SuggestionProps,
} from '@tiptap/suggestion'
import { PluginKey } from '@tiptap/pm/state'
import { InlineMath, BlockMath } from './MarkdownMath'
import {
  AnnotationDecorations,
  annotationKey,
  annotationFlashKey,
  locateAnchor,
  type AnchorQuery,
  type AnnotationAnchor,
} from '../lib/annotationDecorations'
import { InlineAnnotator, type InlineAnnotatorHandle } from './InlineAnnotator'
import type { Annotation, DocKind } from '../lib/useAnnotations'
import { EntityPrefix } from '../lib/entityPrefixExtension'
import { isPrefixCandidate, lookupPrefix } from '../lib/prefixLookup'
import { api, authFetch, workspaceHeader } from '../api/client'
import { formatMentionHref, type MentionKind } from '../lib/mentions'
import { useAttachmentImage } from '../lib/useAuthBlobUrl'
import { invalidateAttachmentManifest } from '../lib/attachmentManifest'
import {
  ACCEPTED_IMAGE_MIME,
  isAcceptedImage,
  uploadImage,
  type ImageUploadParent,
  type UploadedAttachment,
} from '../lib/imageUpload'
import {
  attachmentMarkdownRef,
  attachmentRefFor,
  parseAttachmentMarkdownRef,
  type AttachmentRef,
} from '../lib/attachmentRef'
import { AttachmentPicker } from './AttachmentPicker'

// Remembered show/hide state of the formatting toolbar (one switch for
// all editors). Defaults to shown; collapsing is the opt-in for a
// roomier writing surface, notably on a phone.
const TOOLBAR_PREF_KEY = 'mycelium.rte.toolbar'
function readToolbarPref(): boolean {
  try {
    return localStorage.getItem(TOOLBAR_PREF_KEY) !== '0'
  } catch {
    return true
  }
}

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
// EvidenceMentionExtension: type @ -> search Mycelium tasks/tags ->
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
  name: 'myceliumMention',
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

// EntityAutocomplete (ADR-0038 layer E): type ``[[`` then a title or a
// UUID prefix -> autocomplete of matching tasks / notes -> inserts the
// backtick-prefix code span (the pervasive roadmap convention). The
// EntityPrefix decoration then renders it as a live chip and the
// markdown round-trip serializes it as `` `91cf6aaa` ``. This is the
// authoring counterpart of that read-side chip: it prevents wrong
// references at the source. Distinct from the @-mention above, which
// inserts a title-baked link; a backtick-prefix resolves its title
// live, so it never goes stale.

type EntityCand = { kind: 'task' | 'note'; id: string; label: string }

async function searchEntities(query: string): Promise<EntityCand[]> {
  const q = query.trim()
  const out: EntityCand[] = []
  const seen = new Set<string>()
  const take = (kind: 'task' | 'note', id: string, label: string) => {
    const key = `${kind}:${id}`
    if (seen.has(key)) return
    seen.add(key)
    out.push({ kind, id, label })
  }
  // A hex prefix resolves deterministically via /lookup, shown first.
  if (isPrefixCandidate(q)) {
    const res = await lookupPrefix(q.toLowerCase(), { kinds: ['task', 'note'] })
    for (const m of res?.matches ?? []) take(m.kind, m.id, m.title ?? m.id)
  }
  // Title substring over the workspace's tasks + notes (same instant
  // client-side filter the @-mention and the Cmd+K palette use).
  const h = workspaceHeader()
  const [tk, nt] = await Promise.all([
    api.GET('/tasks', { params: { header: h } }),
    api.GET('/notes', { params: { header: h } }),
  ])
  const lc = q.toLowerCase()
  for (const t of tk.data ?? []) {
    if (!lc || t.title.toLowerCase().includes(lc)) take('task', t.id, t.title)
  }
  for (const n of nt.data ?? []) {
    const label = n.title ?? n.kind
    if (!lc || label.toLowerCase().includes(lc)) take('note', n.id, label)
  }
  return out.slice(0, 8)
}

// Imperative floating listbox for the [[ suggestion. Mirrors the
// inline render MentionExt uses (same .mention-pop styling + keyboard
// model); kept separate so the working @-mention render is untouched.
function entitySuggestionRender() {
  let box: HTMLDivElement | null = null
  let list: EntityCand[] = []
  let sel = 0
  let pick: ((c: EntityCand) => void) | null = null

  const draw = () => {
    const el = box
    if (!el) return
    el.innerHTML = ''
    list.forEach((c, i) => {
      const row = document.createElement('div')
      row.className =
        'mention-pop__row' + (i === sel ? ' mention-pop__row--sel' : '')
      row.id = `entity-opt-${i}`
      row.setAttribute('role', 'option')
      row.setAttribute('aria-selected', i === sel ? 'true' : 'false')
      row.textContent = `${c.kind === 'task' ? '✓' : '◆'} ${c.label}`
      row.addEventListener('mousedown', (e) => {
        e.preventDefault()
        pick?.(c)
      })
      el.append(row)
    })
    if (list.length > 0)
      el.setAttribute('aria-activedescendant', `entity-opt-${sel}`)
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
    onStart: (p: SuggestionProps<EntityCand>) => {
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
    onUpdate: (p: SuggestionProps<EntityCand>) => {
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
}

const EntityAutocompleteExt = Extension.create({
  name: 'myceliumEntityAutocomplete',
  addProseMirrorPlugins() {
    return [
      Suggestion<EntityCand>({
        editor: this.editor,
        // Distinct key: @tiptap/suggestion defaults every instance to the
        // 'suggestion' PluginKey, so without this the @-mention plugin and
        // this one collide ("Adding different instances of a keyed plugin").
        pluginKey: new PluginKey('myceliumEntityAutocompleteSuggestion'),
        char: '[[',
        allowSpaces: false,
        startOfLine: false,
        items: ({ query }) => searchEntities(query),
        command: ({ editor, range, props }) => {
          // Insert the 8-char id prefix as inline code (the backtick
          // convention), then a trailing unmarked space so typing
          // continues outside the code mark.
          const code = props.id.replace(/-/g, '').slice(0, 8)
          editor
            .chain()
            .focus()
            .insertContentAt(range, [
              { type: 'text', text: code, marks: [{ type: 'code' }] },
              { type: 'text', text: ' ' },
            ])
            .run()
        },
        render: entitySuggestionRender,
      }),
    ]
  },
})

// Live preview of an embedded image inside the editor. The src is either
// "/attachments/<id>/download" (bearer-auth route, inserted by the
// picker/upload) or a bare filename the author typed for a file uploaded
// to this note/task; useAttachmentImage resolves the latter against the
// parent's attachments (read from the extension storage set by the host
// editor) and auth-fetches either way. An unresolvable reference shows a
// broken-image placeholder instead of spinning forever.
function ImageNodeView({ node, extension }: NodeViewProps) {
  const src = typeof node.attrs.src === 'string' ? node.attrs.src : ''
  const alt = typeof node.attrs.alt === 'string' ? node.attrs.alt : ''
  const title = typeof node.attrs.title === 'string' ? node.attrs.title : undefined
  const getParent = extension.options.getParent as
    | (() => ImageUploadParent | undefined)
    | undefined
  const parent = getParent?.()
  const { url, loading } = useAttachmentImage(src, parent)
  if (loading) {
    return <NodeViewWrapper as="span" className="md-img md-img--loading" />
  }
  if (!url) {
    return (
      <NodeViewWrapper as="span" className="md-img md-img--broken">
        {alt || src || '?'}
      </NodeViewWrapper>
    )
  }
  return (
    <NodeViewWrapper as="span" className="md-img-wrap">
      <img src={url} alt={alt} title={title} className="md-img" />
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
  // The host editor injects a getter for the owning note/task so the
  // node view can resolve `![alt](filename.png)` references against its
  // attachments. A getter (not a static value) keeps it current as the
  // parent id arrives after a first save.
  addOptions() {
    return { getParent: () => undefined as ImageUploadParent | undefined }
  },
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

// Imperative surface for navigating to an annotation's anchored passage
// from outside the editor (the AnnotationsPanel's "go to text" button).
// The host shares one ref between the editor and the panel; see
// PartAnnotated / TaskDetailRoute.
export interface AnnotationViewHandle {
  /** Scroll the editor so the annotation's anchored text is in view and
   * briefly flash it. Returns false when there is nothing to jump to
   * (raw mode, editor not ready, or the passage no longer exists). */
  scrollToAnnotation: (a: Annotation) => boolean
}

// An annotation the toolbar ▲/▼ walks: OPEN only (resolved comments and
// accepted/rejected suggestions are excluded — they carry no live mark in
// the prose), not deleted, and with anchor text to locate. The panel's
// per-card ⌖ is separate and still jumps to resolved/rejected ones.
function isNavigableAnnotation(a: Annotation): boolean {
  return (
    !a.deleted_at &&
    a.status === 'open' &&
    (a.kind === 'suggestion' ? !!a.original_text : !!a.anchor_quote)
  )
}

// Map an annotation row to the minimal shape locateAnchor needs.
function anchorOf(a: Annotation): AnchorQuery {
  return {
    kind: a.kind === 'suggestion' ? 'suggestion' : 'comment',
    anchorQuote: a.anchor_quote ?? null,
    anchorPrefix: a.anchor_prefix ?? null,
    anchorSuffix: a.anchor_suffix ?? null,
    originalText: a.original_text ?? null,
  }
}

export function RichEditor({
  value,
  onChange,
  placeholder,
  large,
  imageUploadParent,
  filename,
  annotations,
  inlineAnnotations,
  viewRef,
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
  // When provided, the editor gains the inline annotation UX: a floating
  // 💬/✎ toolbar on the live selection (create) and a click-on-mark
  // action popover (accept/reject/resolve/edit/reply/delete). The host
  // supplies the shared useAnnotations rows + reload so the panel, the
  // decorations and this layer stay in sync.
  inlineAnnotations?: {
    docKind: DocKind
    docId: string
    rows: Annotation[]
    reload: () => Promise<void>
    onDocMutated?: () => void | Promise<void>
    allowSuggest?: boolean
  }
  // Shared by the host with its AnnotationsPanel so the panel can scroll
  // the editor to a comment/suggestion's anchored text. Populated below
  // via useImperativeHandle.
  viewRef?: Ref<AnnotationViewHandle>
}) {
  const { t } = useTranslation()
  // Drop to a plain markdown textarea (paste long blocks, fix a bad
  // round-trip). Both modes read/write the same `value` markdown
  // string (bitvision EvidenceEditor pattern).
  const [rawMode, setRawMode] = useState(false)
  // Collapse the formatting buttons to reclaim writing space (a single
  // tap on a phone, where the wrapped bar otherwise eats several rows).
  // Persisted so the choice sticks across editors and sessions.
  const [showTools, setShowTools] = useState(readToolbarPref)
  useEffect(() => {
    try {
      localStorage.setItem(TOOLBAR_PREF_KEY, showTools ? '1' : '0')
    } catch {
      // Private mode / storage disabled: the toggle still works for the
      // session, it just won't be remembered.
    }
  }, [showTools])
  // The inline-annotation layer reports whether the editor has a
  // non-empty selection, so the toolbar's Comment / Suggest buttons
  // enable only when there is something to annotate.
  const [canAnnotate, setCanAnnotate] = useState(false)
  // "Go to %" toolbar field (note-part position navigation).
  const [pctInput, setPctInput] = useState('')
  const annoRef = useRef<InlineAnnotatorHandle>(null)
  const [uploadErr, setUploadErr] = useState<string | null>(null)
  const [uploading, setUploading] = useState(false)
  const [pickerOpen, setPickerOpen] = useState(false)
  const [pdfBusy, setPdfBusy] = useState(false)
  const [pdfErr, setPdfErr] = useState<string | null>(null)
  const imgInput = useRef<HTMLInputElement>(null)
  const rawRef = useRef<HTMLTextAreaElement>(null)
  // Latest rawMode for the scroll handle (built once, must see the live
  // value): in raw mode the WYSIWYG DOM is detached, so there is nothing
  // to scroll to.
  const rawModeRef = useRef(rawMode)
  useEffect(() => {
    rawModeRef.current = rawMode
  }, [rawMode])
  // Single coalesced timer that clears the flash decoration; a new flash
  // (repeat click or prev/next step) cancels the prior clear before
  // re-arming so the pulse always runs its full duration.
  const flashClearRef = useRef<number | null>(null)
  // Cursor for the toolbar's prev/next navigation, tracked by annotation
  // ID (not a raw index): the ordered list is rebuilt on every step from
  // the live rows, so after a resolve/accept/delete/reload the previous
  // index would point into a stale, possibly shorter list and jump to the
  // wrong annotation. Re-finding the current ID keeps the step relative to
  // where the user actually is; null means "not started".
  const navIdRef = useRef<string | null>(null)
  // Transient "the open annotation's passage is gone" hint for the toolbar
  // prev/next, mirroring the panel's per-card ⌖ miss: shown when there are
  // open annotations to walk but none can be located in the live prose
  // (their quoted text was edited away), so the click isn't a silent no-op.
  const [navMiss, setNavMiss] = useState(false)
  const navMissTimer = useRef<number | null>(null)

  // Keep the latest parent in a ref so the editorProps handlers below
  // (created once when the editor is built) see the live value even if
  // the prop changes mid-session (e.g. a freshly created note id
  // arriving from the save call).
  const parentRef = useRef<ImageUploadParent | undefined>(imageUploadParent)
  useEffect(() => {
    parentRef.current = imageUploadParent
  }, [imageUploadParent])

  // Insert a markdown snippet at the raw-mode caret (or append at the
  // end if not focused). The WYSIWYG branch goes through the editor's
  // insertContent below.
  const insertRawSnippet = (md: string) => {
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

  // Whether the caret sits somewhere that must keep pasted text VERBATIM:
  // inside a fenced code block (```mermaid included, it is a code node with
  // its own live preview) or inside an inline `code` mark. Converting a
  // reference to a node there would silently corrupt a snippet whose whole
  // point is to show the markdown syntax -- which is exactly what a user
  // documenting the reference format is doing. Keyed on the schema's own
  // ``spec.code`` rather than on an extension name, so a future code-ish
  // node is covered without touching this.
  const inCodeContext = (ed: CoreEditor): boolean => {
    if (ed.isActive('code')) return true
    const parent = ed.state.selection.$from.parent
    return parent.type.spec.code === true
  }

  // Insert an attachment reference as a NODE at the WYSIWYG caret: an
  // image embed for an image attachment, a link otherwise. Both carry the
  // same bearer-auth /attachments href (resolved through authFetch at
  // render time); neither exposes a public URL. Either node serializes
  // back through tiptap-markdown as the same `![name](href)` /
  // `[name](href)` reference attachmentMarkdownRef emits.
  const insertAttachmentNode = (ed: CoreEditor, ref: AttachmentRef) => {
    if (ref.image) {
      ed.chain()
        .focus()
        .insertContent({
          type: 'image',
          attrs: { src: ref.href, alt: ref.label, title: null },
        })
        .run()
      return
    }
    // Text node carrying a link mark, plus a trailing unmarked space so
    // the caret lands outside the link and typing does not extend it.
    ed.chain()
      .focus()
      .insertContent([
        {
          type: 'text',
          text: ref.label,
          marks: [{ type: 'link', attrs: { href: ref.href } }],
        },
        { type: 'text', text: ' ' },
      ])
      .run()
  }

  // Insert a reference to the attachment picked in the AttachmentPicker.
  // Raw mode takes the markdown string, WYSIWYG the equivalent node —
  // both derived from attachmentRef.ts, so this path and the Attachments
  // panel's "Copy ref" cannot drift on the image predicate or the route.
  const insertRef = (att: UploadedAttachment) => {
    const meta = {
      id: att.id,
      filename: att.filename,
      mime_type: att.mimeType,
    }
    if (rawMode) {
      insertRawSnippet(attachmentMarkdownRef(meta))
      return
    }
    const ed = editorRef.current
    if (!ed) return
    insertAttachmentNode(ed, attachmentRefFor(meta))
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
      // A new file exists now; drop the cached name->id map so a later
      // `![alt](filename)` reference to it resolves.
      invalidateAttachmentManifest(parent)
      if (rawMode) {
        insertRawSnippet(`![${up.filename}](${up.url})`)
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
        const data = event.clipboardData
        // Pasted image FILE -> upload it as an attachment of this
        // note/task (hence the parent gate) and embed the result.
        const items = parentRef.current ? data?.items : undefined
        if (items) {
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
        }
        // Pasted attachment REFERENCE -> the node it denotes. This is the
        // string the Attachments panel's "Copy ref", the MCP `attach_file`
        // tool and the CLI hand over, and pasting it is exactly how a user
        // consumes it. Without this it would land as a literal text node
        // and be saved with its brackets backslash-escaped
        // (`!\[name\](/attachments/…)`), so every reader would get those
        // raw characters instead of the image or the link.
        // The reference carries the attachment id, so unlike the file
        // branch above this needs no parent.
        // Anything that is not one whole reference (parse returns null)
        // falls through to ProseMirror's own paste, unchanged.
        const ed = editorRef.current
        const ref = parseAttachmentMarkdownRef(data?.getData('text/plain') ?? '')
        if (ed && ref && !inCodeContext(ed)) {
          event.preventDefault()
          insertAttachmentNode(ed, ref)
          return true
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
      // Disable StarterKit's bundled code block: CodeBlockMermaid below
      // replaces it (same node name + ```fence round-trip) so a ```mermaid
      // block renders its diagram live while every other code block keeps
      // the default behaviour.
      StarterKit.configure({ link: false, codeBlock: false }),
      Link.configure({
        openOnClick: false,
        autolink: false,
        // Accept the @kind:uuid mention DSL hrefs AND the relative
        // /attachments/<id>/download route in addition to regular links,
        // so [label](@note:uuid) and [file.pdf](/attachments/<id>/download)
        // survive the markdown round-trip as a clickable link. Without
        // the /attachments arm the Link mark is silently stripped to bare
        // text on parse-back — the link would vanish on save/reload.
        validate: (url: string) => {
          if (!url) return false
          if (/^@(?:task|note|tag):/.test(url)) return true
          if (/^\/attachments\//.test(url)) return true
          // A bare filename ref to an attachment of this note/task, e.g.
          // [report.pdf](report.pdf): no scheme/leading slash, no spaces,
          // ending in an extension. Kept so the link survives the
          // markdown round-trip; the click-interceptor resolves it.
          if (/^[^\s:/][^\s:]*\.[A-Za-z0-9]{1,12}$/.test(url)) return true
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
      // Code blocks, with a live diagram preview for ```mermaid (replaces
      // StarterKit's codeBlock, disabled above).
      CodeBlockMermaid,
      Markdown.configure({ html: false }),
      // LaTeX math (``$inline$`` and ``$$block$$``): KaTeX NodeView in
      // the editor + markdown-it round-trip, so what /garden's
      // MarkdownView renders read-side is what the author saw write-side.
      InlineMath,
      BlockMath,
      // Embedded images via /attachments uploads. Round-trips to the
      // standard `![alt](src)` markdown; the node view auth-fetches the
      // bytes for live preview and resolves bare-filename refs against
      // the parent's attachments (read live through parentRef). The
      // getter is invoked later from the node view, never during render.
      // eslint-disable-next-line react-hooks/refs
      ImageExt.configure({ getParent: () => parentRef.current }),
      MentionExt,
      EntityAutocompleteExt,
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

  // Scroll the located range into view (without disturbing the selection /
  // caret) and pulse it via a real PM decoration. A decoration survives
  // ProseMirror's view reconciliation, unlike a class hand-added to a
  // decoration node (which PM reverts on its next update). The clear is
  // coalesced so a rapid repeat / prev-next sequence always pulses fully.
  const flashRange = useCallback((ed: CoreEditor, r: { from: number; to: number }) => {
    const dom = ed.view.domAtPos(r.from)
    // nodeType 3 = Text; ``Node`` is shadowed by @tiptap/core's import here,
    // so use the numeric constant.
    const el = dom.node.nodeType === 3 ? dom.node.parentElement : (dom.node as HTMLElement)
    el?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    ed.view.dispatch(ed.state.tr.setMeta(annotationFlashKey, r))
    if (flashClearRef.current !== null) window.clearTimeout(flashClearRef.current)
    flashClearRef.current = window.setTimeout(() => {
      const e = editorRef.current
      if (e) e.view.dispatch(e.state.tr.setMeta(annotationFlashKey, null))
      flashClearRef.current = null
    }, 1500)
  }, [])

  // Annotations that have a locatable passage, in document order — the
  // domain both the panel's "go to text" and the toolbar prev/next walk.
  const orderedAnchored = useCallback(() => {
    const ed = editorRef.current
    if (!ed) return [] as { row: Annotation; from: number; to: number }[]
    return (inlineAnnotations?.rows ?? [])
      .filter(isNavigableAnnotation)
      .map((row) => ({ row, r: locateAnchor(ed.state.doc, anchorOf(row)) }))
      .flatMap((x) => (x.r ? [{ row: x.row, from: x.r.from, to: x.r.to }] : []))
      .sort((a, b) => a.from - b.from)
  }, [inlineAnnotations?.rows])

  // Step to the next/previous anchored annotation in document order and
  // flash it; wraps at the ends. ``dir`` is +1 (▼, first→last) or -1 (▲).
  const navigateAnnotations = useCallback(
    (dir: 1 | -1) => {
      const ed = editorRef.current
      if (!ed || rawModeRef.current) return
      const items = orderedAnchored()
      if (!items.length) {
        // No locatable passage to jump to. If open annotations exist but
        // their anchored text drifted away, surface the same "not found"
        // hint the panel shows instead of a silent dead click.
        if ((inlineAnnotations?.rows ?? []).some(isNavigableAnnotation)) {
          setNavMiss(true)
          if (navMissTimer.current !== null) window.clearTimeout(navMissTimer.current)
          navMissTimer.current = window.setTimeout(() => {
            setNavMiss(false)
            navMissTimer.current = null
          }, 2500)
        }
        return
      }
      setNavMiss(false)
      // Re-find the current annotation by ID in the freshly rebuilt list,
      // then step relative to it; -1 (gone / not started) makes ▼ land on
      // the first and ▲ on the last.
      const cur = navIdRef.current
        ? items.findIndex((x) => x.row.id === navIdRef.current)
        : -1
      let idx = cur + dir
      if (idx < 0) idx = items.length - 1
      if (idx >= items.length) idx = 0
      navIdRef.current = items[idx].row.id
      flashRange(ed, items[idx])
      // Reveal the comment/suggestion body, not just its passage: the
      // toolbar buttons read "go to the next comment", so show it.
      annoRef.current?.openAnnotation(items[idx].row.id)
    },
    [orderedAnchored, flashRange, inlineAnnotations?.rows],
  )

  // Imperative "go to this annotation" for the panel's per-card button:
  // locate the passage (live marks or resolved/rejected alike), scroll +
  // flash, and sync the toolbar nav cursor so a subsequent ▼/▲ continues
  // from here. Returns false when there is nothing to jump to.
  useImperativeHandle(
    viewRef,
    () => ({
      scrollToAnnotation: (a: Annotation) => {
        const ed = editorRef.current
        if (!ed || rawModeRef.current) return false
        const r = locateAnchor(ed.state.doc, anchorOf(a))
        if (!r) return false
        // Sync the toolbar nav cursor so a subsequent ▼/▲ continues from
        // the card the user jumped to (by ID; if it's resolved and thus
        // outside the open-only walk, the next step restarts from the end).
        navIdRef.current = a.id
        flashRange(ed, r)
        return true
      },
    }),
    [flashRange],
  )

  // Any OPEN anchored annotation to navigate? Gates the toolbar prev/next.
  const hasAnchoredAnnotations = (inlineAnnotations?.rows ?? []).some(isNavigableAnnotation)

  // Scroll the editor to a fraction of the part: 0 = start, 1 = end, and
  // anything in between maps to that proportion of the document (by
  // position, a robust proxy for "N% through the note" that works whether
  // the editor scrolls the page or a modal body). Selection/caret left
  // untouched. Powers the toolbar's start / end / "go to %" controls,
  // handy on long note parts. In raw mode the textarea scrolls its own
  // content proportionally (it has an inner scrollbar), so the controls
  // keep working there too.
  const goToFraction = useCallback((f: number) => {
    const clamped = Math.max(0, Math.min(1, f))
    if (rawModeRef.current) {
      const ta = rawRef.current
      if (!ta) return
      ta.scrollTop = clamped * (ta.scrollHeight - ta.clientHeight)
      ta.scrollIntoView({
        behavior: 'smooth',
        block: clamped <= 0 ? 'start' : clamped >= 1 ? 'end' : 'center',
      })
      return
    }
    const ed = editorRef.current
    if (!ed) return
    if (clamped <= 0) {
      // Some hosts (the note modal) make the ProseMirror element its own
      // scroll container; scrollIntoView on a scroll container only moves
      // its ancestors, so also reset its own scrollTop (a no-op when the
      // page scrolls instead).
      ed.view.dom.scrollTo({ top: 0, behavior: 'smooth' })
      ed.view.dom.scrollIntoView({ behavior: 'smooth', block: 'start' })
      return
    }
    if (clamped >= 1) {
      ed.view.dom.scrollTo({ top: ed.view.dom.scrollHeight, behavior: 'smooth' })
      ed.view.dom.scrollIntoView({ behavior: 'smooth', block: 'end' })
      return
    }
    const size = ed.state.doc.content.size
    const pos = Math.max(1, Math.min(size - 1, Math.round(clamped * size)))
    const dom = ed.view.domAtPos(pos)
    const el = dom.node.nodeType === 3 ? dom.node.parentElement : (dom.node as HTMLElement)
    el?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  }, [])

  const goToPercent = () => {
    const n = Number(pctInput)
    if (!Number.isFinite(n) || pctInput.trim() === '') return
    goToFraction(Math.max(0, Math.min(100, n)) / 100)
  }

  // Drop pending flash-clear / nav-miss timers on unmount so they can't
  // fire against a torn down editor.
  useEffect(
    () => () => {
      if (flashClearRef.current !== null) window.clearTimeout(flashClearRef.current)
      if (navMissTimer.current !== null) window.clearTimeout(navMissTimer.current)
    },
    [],
  )

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
      // Lets the global click-interceptor resolve a bare-filename
      // attachment link typed in this editor against the right note/task.
      data-attachment-parent={
        imageUploadParent
          ? `${imageUploadParent.kind}:${imageUploadParent.id}`
          : undefined
      }
    >
      <div className="rte__bar">
        <div className="rte__bar-left">
          <button
            type="button"
            className="btn--ghost btn--sm rte__collapse"
            aria-expanded={showTools}
            title={
              showTools
                ? t('editor.toolbarHide', { defaultValue: 'Hide toolbar' })
                : t('editor.toolbarShow', { defaultValue: 'Show toolbar' })
            }
            onClick={() => setShowTools((v) => !v)}
          >
            {showTools ? '⌄' : 'Aa'}
          </button>
          {/* Position navigation: jump to the start / end of the document,
              or type a percentage (e.g. 30) to land ~30% through it.
              Deliberately OUTSIDE the collapsible tools span and not gated
              on the annotation layer: every editor (comment cards and
              composers included) keeps these reachable whenever the bar is
              visible, in WYSIWYG and raw mode alike. */}
          <button
            type="button"
            className="btn--ghost btn--sm rte__fmt rte__goto"
            title={t('editor.goToStart', { defaultValue: 'Go to the start' })}
            aria-label={t('editor.goToStart', { defaultValue: 'Go to the start' })}
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => goToFraction(0)}
          >
            ⤒
          </button>
          <button
            type="button"
            className="btn--ghost btn--sm rte__fmt rte__goto"
            title={t('editor.goToEnd', { defaultValue: 'Go to the end' })}
            aria-label={t('editor.goToEnd', { defaultValue: 'Go to the end' })}
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => goToFraction(1)}
          >
            ⤓
          </button>
          <input
            type="number"
            min={0}
            max={100}
            className="rte__goto-pct"
            value={pctInput}
            placeholder="%"
            title={t('editor.goToPercent', {
              defaultValue: 'Go to a percentage of the document (press Enter)',
            })}
            aria-label={t('editor.goToPercent', {
              defaultValue: 'Go to a percentage of the document (press Enter)',
            })}
            onMouseDown={(e) => e.stopPropagation()}
            onChange={(e) => setPctInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault()
                goToPercent()
              }
            }}
          />
          {showTools && (
          <span className="rte__tools">
          {/* Annotation triggers, when this editor carries the inline
              comment/suggestion layer. They drive the InlineAnnotator
              through its imperative handle on the current selection, so
              they replace the old floating-on-selection bubble: always in
              the (sticky) bar, never hunting for a transient popover. */}
          {inlineAnnotations && (
            <>
              <button
                type="button"
                className="btn--ghost btn--sm rte__fmt rte__annotate rte__annotate--comment"
                title={t('editor.annotateComment', {
                  defaultValue: 'Comment on the selected text',
                })}
                disabled={!canAnnotate}
                // Keep the editor selection: a plain click would blur and
                // collapse it before the handler reads it.
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => annoRef.current?.openComment()}
              >
                💬
              </button>
              {inlineAnnotations.allowSuggest !== false && (
                <button
                  type="button"
                  className="btn--ghost btn--sm rte__fmt rte__annotate rte__annotate--suggest"
                  title={t('editor.annotateSuggest', {
                    defaultValue: 'Suggest an edit to the selected text',
                  })}
                  disabled={!canAnnotate}
                  onMouseDown={(e) => e.preventDefault()}
                  onClick={() => annoRef.current?.openSuggest()}
                >
                  ✎
                </button>
              )}
              {/* Walk the document's comments/suggestions in order: ▲ to the
                  previous anchored annotation, ▼ to the next (first→last).
                  Enabled only when there is at least one to jump to. */}
              <button
                type="button"
                className="btn--ghost btn--sm rte__fmt rte__annotate rte__annotate--nav"
                title={t('editor.annotatePrev', {
                  defaultValue: 'Go to the previous comment / suggestion',
                })}
                aria-label={t('editor.annotatePrev', {
                  defaultValue: 'Go to the previous comment / suggestion',
                })}
                disabled={!hasAnchoredAnnotations}
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => navigateAnnotations(-1)}
              >
                ↑
              </button>
              <button
                type="button"
                className="btn--ghost btn--sm rte__fmt rte__annotate rte__annotate--nav"
                title={t('editor.annotateNext', {
                  defaultValue: 'Go to the next comment / suggestion',
                })}
                aria-label={t('editor.annotateNext', {
                  defaultValue: 'Go to the next comment / suggestion',
                })}
                disabled={!hasAnchoredAnnotations}
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => navigateAnnotations(1)}
              >
                ↓
              </button>
              {navMiss && (
                <span className="anno__locate-miss" role="status">
                  {t('annotations.anchorNotFound', {
                    defaultValue: 'Text not found in the document',
                  })}
                </span>
              )}
              {/* Divider: annotation navigation vs the format buttons. */}
              <span className="rte__sep" aria-hidden="true" />
            </>
          )}
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
          <button
            type="button"
            className="btn--ghost btn--sm rte__fmt"
            title={
              imageUploadParent
                ? t('editor.attach')
                : t('editor.attachNeedsSave')
            }
            disabled={!imageUploadParent}
            onClick={() => setPickerOpen(true)}
          >
            📎
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
          )}
        </div>
        <span className="rte__actions">
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
      {pickerOpen && imageUploadParent && (
        <AttachmentPicker
          parent={imageUploadParent}
          onPick={insertRef}
          onClose={() => setPickerOpen(false)}
        />
      )}
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
      {editor && !rawMode && inlineAnnotations && (
        <InlineAnnotator
          ref={annoRef}
          editor={editor}
          docKind={inlineAnnotations.docKind}
          docId={inlineAnnotations.docId}
          rows={inlineAnnotations.rows}
          reload={inlineAnnotations.reload}
          onDocMutated={inlineAnnotations.onDocMutated}
          allowSuggest={inlineAnnotations.allowSuggest}
          onSelectableChange={setCanAnnotate}
          parent={imageUploadParent}
        />
      )}
    </div>
  )
}
