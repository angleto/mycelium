import { useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState, type Ref } from 'react'
import { useTranslation } from 'react-i18next'
import { EditorView as CmEditorView, type EditorView as CmView } from '@codemirror/view'
import { InlineAnnotator, type InlineAnnotatorHandle } from './InlineAnnotator'
import { anchorDomainOf, type Annotation, type DocKind } from '../lib/useAnnotations'
import { sourceSurface, type AnnotationSurface } from '../lib/annotationSurface'
import {
  flashAnnotation,
  locateSourceAnchor,
  setAnnotations as setSourceAnnotations,
  type AnchorQuery,
  type AnnotationAnchor,
} from '../lib/markdownSource/annotationLayer'
import { authFetch } from '../api/client'
import { invalidateAttachmentManifest } from '../lib/attachmentManifest'
import {
  ACCEPTED_IMAGE_MIME,
  isAcceptedImage,
  uploadImage,
  type ImageUploadParent,
} from '../lib/imageUpload'
import { attachmentMarkdownRef } from '../lib/attachmentRef'
import { AttachmentPicker } from './AttachmentPicker'
import { renderMarkdownToHtml } from '../lib/markdownSource/renderForPrint'
import { mdLink } from '../lib/markdownInline'
import {
  downloadText,
  filenameFromContentDisposition,
  sanitizeFilename,
} from '../lib/downloadFile'
import {
  SourceEditor,
  type SourceEditorHandle,
} from '../lib/markdownSource/SourceEditor'
import {
  insertHorizontalRule as srcHr,
  insertTable as srcInsertTable,
  setLink as srcSetLink,
  toggleBulletList as srcBullet,
  toggleCodeBlock as srcCodeBlock,
  toggleHeading as srcHeading,
  toggleOrderedList as srcOrdered,
  toggleQuote as srcQuote,
  toggleTaskList as srcTask,
  toggleWrap as srcWrap,
  redoCommand as srcRedo,
  undoCommand as srcUndo,
  type ActiveMark,
} from '../lib/markdownSource/commands'
import {
  addColumnAfter as srcAddCol,
  addRowAfter as srcAddRow,
  deleteTable as srcDelTable,
  formatTable as srcFormatTable,
} from '../lib/markdownSource/tableCommands'

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

function blobToDataUrl(blob: Blob): Promise<string> {
  return new Promise<string>((resolve, reject) => {
    const r = new FileReader()
    r.onload = () => resolve(String(r.result))
    r.onerror = () => reject(r.error)
    r.readAsDataURL(blob)
  })
}

// Make every image in the printable copy self-contained.
//
// Two shapes reach here, and the second one only started to once the export
// began rendering through the READ-SIDE renderer.
//
// A bearer-auth ``/attachments/...`` path 401s from inside a print iframe:
// no Authorization header propagates, so the bytes are fetched with the
// SPA's authFetch and inlined.
//
// A ``blob:`` URL is what the reader's own <img> carries -- useAttachmentImage
// has already fetched the bytes and wrapped them in an object URL. That URL
// belongs to THIS document and means nothing to the backend, so it has to be
// read back and inlined too, or every attachment image would silently vanish
// from the PDF.
async function inlineAuthImages(html: string): Promise<string> {
  const tmp = document.createElement('div')
  tmp.innerHTML = html
  const imgs = Array.from(tmp.querySelectorAll('img'))
  await Promise.all(
    imgs.map(async (img) => {
      const src = img.getAttribute('src')
      if (!src) return
      const isAuthPath = src.startsWith('/')
      const isBlob = src.startsWith('blob:')
      if (!isAuthPath && !isBlob) return
      try {
        const res = isBlob ? await fetch(src) : await authFetch(src)
        if (!res.ok) return
        img.setAttribute('src', await blobToDataUrl(await res.blob()))
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
  ) || `${sanitizeFilename(title)}.pdf`
  a.rel = 'noopener'
  document.body.append(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 0)
}

// True WYSIWYG (no preview toggle), markdown round-trip via
// tiptap-markdown, and an inline @ typeahead mirroring bitvision's
// EvidenceMentionExtension: type @ -> search Mycelium tasks/tags ->
// inserts a [label](@kind:id) link that serializes as the DSL.

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
    anchorDomain: anchorDomainOf(a),
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
  // STATE, not a ref: whether the source editor exists decides whether the
  // toolbar's buttons are enabled, and that is render-relevant information.
  // A ref would have to be read during render to answer it, which is both
  // what react-hooks/refs forbids and, here, exactly why: the toolbar would
  // not re-render when the editor mounted.
  const [srcHandle, setSrcHandle] = useState<SourceEditorHandle | null>(null)
  // Mirrored for the callbacks built once for the component's lifetime
  // (goToFraction), which must see the live handle rather than the one that
  // existed when they were created.
  const srcHandleRef = useRef<SourceEditorHandle | null>(null)
  useEffect(() => {
    srcHandleRef.current = srcHandle
  }, [srcHandle])
  // Which constructs the caret is inside in SOURCE mode, reported by the
  // editor on every selection change. The tiptap path asks the editor
  // synchronously (``editor.isActive``); CodeMirror has no equivalent that
  // re-renders React, so the state is pushed instead of pulled.
  const [srcMarks, setSrcMarks] = useState<Set<ActiveMark>>(() => new Set())
  // Latest ``value`` for the editor's own onUpdate closure (built once):
  // the emit path re-attaches the trailing newline the incoming body
  // carries, so it has to read the live one, not the mount-time one.
  const valueRef = useRef(value)
  useEffect(() => {
    valueRef.current = value
  }, [value])
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
    const src = srcHandle
    if (!src) {
      onChange(value + (value.endsWith('\n') || !value ? '' : '\n') + md + '\n')
      return
    }
    // The source editor owns the caret, the change and the scroll: it
    // dispatches one transaction instead of rewriting the whole value and
    // then racing a requestAnimationFrame to put the caret back.
    src.insert(md)
  }

  // Whether the caret sits somewhere that must keep pasted text VERBATIM:
  // inside a fenced code block (```mermaid included, it is a code node with
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
      insertRawSnippet(mdLink(up.filename, up.url, { image: true }))
    } catch (e) {
      setUploadErr(e instanceof Error ? e.message : String(e))
    } finally {
      setUploading(false)
    }
  }

  // Images pasted or dropped onto the source editor upload and land as a
  // markdown reference. Returns whether the event was consumed: anything
  // that is not an uploadable image falls through to CodeMirror, which is
  // what still pastes the text half of a mixed payload.
  const handleDroppedFiles = (files: File[]): boolean => {
    if (!parentRef.current) return false
    const images = files.filter(isAcceptedImage)
    if (!images.length) return false
    for (const f of images) void doUpload(f)
    return true
  }

  // Locate an anchor in the document. An anchor captured in the RETIRED
  // WYSIWYG surface quotes a rendered projection, so it does not resolve
  // here and is refused rather than guessed at: it stays listed in the
  // panel, unpainted, instead of being drawn over a passage nobody chose.
  // Migration 0099 converted every such row it could.
  const locateIn = useCallback((a: AnchorQuery): { from: number; to: number } | null => {
    const view = srcHandleRef.current?.view()
    return view ? locateSourceAnchor(view.state.sliceDoc(), a) : null
  }, [])

  // Scroll a located range into view and pulse it, in the source surface.
  // One dispatch does both: CodeMirror takes the scroll as an effect rather
  // than as a DOM call, so it cannot fight the caret.
  const flashSourceRange = useCallback((r: { from: number; to: number }) => {
    const view = srcHandleRef.current?.view()
    if (!view) return
    view.dispatch({
      effects: [
        flashAnnotation.of(r),
        CmEditorView.scrollIntoView(r.from, { y: 'center' }),
      ],
    })
    if (flashClearRef.current !== null) window.clearTimeout(flashClearRef.current)
    flashClearRef.current = window.setTimeout(() => {
      const v = srcHandleRef.current?.view()
      if (v) v.dispatch({ effects: flashAnnotation.of(null) })
      flashClearRef.current = null
    }, 1500)
  }, [])

  // Annotations that have a locatable passage, in document order — the
  // domain both the panel's "go to text" and the toolbar prev/next walk.
  const orderedAnchored = useCallback(() => {
    return (inlineAnnotations?.rows ?? [])
      .filter(isNavigableAnnotation)
      .map((row) => ({ row, r: locateIn(anchorOf(row)) }))
      .flatMap((x) => (x.r ? [{ row: x.row, from: x.r.from, to: x.r.to }] : []))
      .sort((a, b) => a.from - b.from)
  }, [inlineAnnotations?.rows, locateIn])

  // Step to the next/previous anchored annotation in document order and
  // flash it; wraps at the ends. ``dir`` is +1 (▼, first→last) or -1 (▲).
  const navigateAnnotations = useCallback(
    (dir: 1 | -1) => {
      if (!srcHandleRef.current) return
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
      flashSourceRange(items[idx])
      // Reveal the comment/suggestion body, not just its passage: the
      // toolbar buttons read "go to the next comment", so show it.
      annoRef.current?.openAnnotation(items[idx].row.id)
    },
    [orderedAnchored, flashSourceRange, inlineAnnotations?.rows],
  )

  // Imperative "go to this annotation" for the panel's per-card button:
  // locate the passage (live marks or resolved/rejected alike), scroll +
  // flash, and sync the toolbar nav cursor so a subsequent ▼/▲ continues
  // from here. Returns false when there is nothing to jump to.
  useImperativeHandle(
    viewRef,
    () => ({
      scrollToAnnotation: (a: Annotation) => {
        const r = locateIn(anchorOf(a))
        if (!r) return false
        // Sync the toolbar nav cursor so a subsequent ▼/▲ continues from
        // the card the user jumped to (by ID; if it's resolved and thus
        // outside the open-only walk, the next step restarts from the end).
        navIdRef.current = a.id
        flashSourceRange(r)
        return true
      },
    }),
    [flashSourceRange, locateIn],
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
    srcHandleRef.current?.scrollToFraction(Math.max(0, Math.min(1, f)))
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

  // Push the current annotation anchors onto the editor whenever they
  // change. A pure effect transaction: it carries no document change, so it
  // cannot look like an edit to the autosave.
  useEffect(() => {
    const view = srcHandle?.view()
    if (view) view.dispatch({ effects: setSourceAnnotations.of(annotations ?? []) })
  }, [annotations, srcHandle])

  // Both surfaces format now. In source mode the commands are string
  // transformations (lib/markdownSource/commands.ts) and the handle exists
  // as soon as the editor mounts.
  // The editing surface the annotation UI is layered over.
  const annotationSurface = useMemo<AnnotationSurface | null>(() => {
    const view = srcHandle?.view() ?? null
    return view ? sourceSurface(view) : null
  }, [srcHandle])

  const fmt = srcHandle != null

  const tb = (
    label: string,
    titleKey: string,
    run: (v: CmView) => boolean,
    srcMark?: ActiveMark,
  ) => (
    <button
      type="button"
      className={
        'btn--ghost btn--sm rte__fmt' +
        (srcMark && srcMarks.has(srcMark) ? ' rte__fmt--on' : '')
      }
      title={t(titleKey)}
      disabled={!fmt}
      onClick={() => srcHandle?.run(run)}
    >
      {label}
    </button>
  )

  // Link insert/edit: prompt for a URL (empty clears the link). The
  // source command reads the destination out of the link the caret is in
  // (if any) and writes it back through the shared escaper, so a label
  // containing `]` cannot emit something that is not a link.
  const setLink = () => {
    srcHandle?.run((v) =>
      srcSetLink(v, (current) => window.prompt(t('editor.linkPrompt'), current)),
    )
  }

  // Image button: opens the hidden file input. Disabled when no parent
  // is available (e.g. note-create form before save) — drop/paste are
  // also gated by parentRef inside the handlers.
  const imageDisabled =
    !fmt || uploading || !imageUploadParent
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
          {tb('B', 'editor.bold', srcWrap('bold'), 'bold')}
          {tb('I', 'editor.italic', srcWrap('italic'), 'italic')}
          {tb('S', 'editor.strike', srcWrap('strike'), 'strike')}
          {tb('H1', 'editor.h1', srcHeading(1), 'heading1')}
          {tb('H2', 'editor.h2', srcHeading(2), 'heading2')}
          {tb('H3', 'editor.h3', srcHeading(3), 'heading3')}
          {tb('•', 'editor.bullet', srcBullet, 'bulletList')}
          {tb('1.', 'editor.ordered', srcOrdered, 'orderedList')}
          {tb('☑', 'editor.checklist', srcTask, 'taskList')}
          {tb('❝', 'editor.quote', srcQuote, 'blockquote')}
          {tb('</>', 'editor.code', srcWrap('code'), 'code')}
          {tb('{ }', 'editor.codeBlock', srcCodeBlock, 'codeBlock')}
          <button
            type="button"
            className={'btn--ghost btn--sm rte__fmt' + (srcMarks.has('link') ? ' rte__fmt--on' : '')}
            title={t('editor.link')}
            disabled={!fmt}
            onClick={setLink}
          >
            🔗
          </button>
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
          {tb('―', 'editor.hr', srcHr)}
          <button
            type="button"
            className="btn--ghost btn--sm rte__fmt"
            title={t('editor.table')}
            disabled={!fmt}
            onClick={() => srcHandle?.run(srcInsertTable)}
          >
            ▦
          </button>
          {srcMarks.has('table') && (
            <>
              {tb('+row', 'editor.tableRow', srcAddRow)}
              {tb('+col', 'editor.tableCol', srcAddCol)}
              {tb('✕tbl', 'editor.tableDel', srcDelTable)}
              {/* Re-aligning rewrites bytes nobody typed, so it is a button
                  the author presses and never something Tab does. */}
              {tb('⇔tbl', 'editor.tableFormat', srcFormatTable)}
            </>
          )}
          {tb('↶', 'editor.undo', srcUndo)}
          {tb('↷', 'editor.redo', srcRedo)}
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
              const slug = sanitizeFilename(filename)
              downloadText(`${slug}.md`, 'text/markdown;charset=utf-8', value)
            }}
          >
            {t('editor.downloadMdShort', { defaultValue: '.md' })}
          </button>
          <button
            type="button"
            className="btn--ghost btn--sm"
            disabled={pdfBusy}
            title={t('editor.exportPdf', {
              defaultValue: 'Esporta PDF',
            })}
            onClick={() => {
              const slug = sanitizeFilename(filename)
              setPdfBusy(true)
              setPdfErr(null)
              // Through the READ-SIDE renderer, so the PDF is what the note
              // looks like rather than what a second renderer thought it
              // should look like.
              void renderMarkdownToHtml(value, imageUploadParent)
                .then((html) => exportPdfViaServer(slug, html))
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
          onPick={(meta) => {
            // One insertion path: the markdown reference the rest of the app
            // hands out. The WYSIWYG branch used to convert it into a
            // ProseMirror node instead, which is the asymmetry that made a
            // PASTED reference need its own special case.
            insertRawSnippet(attachmentMarkdownRef(meta))
            setPickerOpen(false)
          }}
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
      <SourceEditor
        handleRef={setSrcHandle}
        className="rte__raw rte__src"
        value={value}
        placeholder={placeholder}
        onChange={onChange}
        onPasteFiles={handleDroppedFiles}
        getParent={() => parentRef.current}
        onActive={setSrcMarks}
      />
      {annotationSurface && inlineAnnotations && (
        <InlineAnnotator
          ref={annoRef}
          surface={annotationSurface}
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
