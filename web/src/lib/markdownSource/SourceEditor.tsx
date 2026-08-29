import { useEffect, useImperativeHandle, useRef, type Ref } from 'react'
import { EditorState } from '@codemirror/state'
import { EditorView } from '@codemirror/view'
import { syncExternal } from './syncExternal'
import { markdownSourceExtensions } from './extensions'
import { activeMarks, type ActiveMark } from './commands'
import { setMarkdownMode, type MarkdownMode } from './mode'
import type { ImageUploadParent } from '../imageUpload'

// The markdown SOURCE editing surface.
//
// Its document is the markdown string itself. Reading it is
// ``state.sliceDoc()`` and nothing else: there is no serializer between what
// the user typed and what gets stored, so there is nothing that can be lossy.
// That is the whole point, and it is why this replaces the plain
// ``<textarea>`` rather than sitting next to it -- everything the textarea
// gave (byte-exactness) it keeps, and it adds the structure a textarea
// cannot have (syntax highlighting now, live preview and source-level
// commands next).
//
// ``sliceDoc()``, NOT ``doc.toString()``: ``Text.toString()`` is
// ``sliceString(0)`` and ``sliceString`` defaults its separator to ``"\n"``,
// while ``EditorState.sliceDoc`` passes ``state.lineBreak``. One word,
// and the difference is whether a CRLF body survives the first keystroke.

export type SourceEditorHandle = {
  /** Insert markdown at the caret, or append it when the editor is not
   *  focused. Leaves the caret after the inserted text. */
  insert: (md: string) => void
  /** Scroll to a fraction of the document (0 = start, 1 = end). */
  scrollToFraction: (f: number) => void
  focus: () => void
  /** Run a source command against the live view. Returns what the command
   *  returned: false means it refused (see commands.ts), and the caller can
   *  leave the document alone rather than guessing. */
  run: (cmd: (view: EditorView) => boolean) => boolean
  /** The live view, for a consumer that needs the surface itself rather than
   *  a command over it -- the annotation layer reads the selection, listens
   *  for a click on a mark, and measures coordinates for its popovers. */
  view: () => EditorView | null
}

export function SourceEditor({
  value,
  onChange,
  mode,
  placeholder,
  className,
  onPasteFiles,
  getParent,
  onActive,
  handleRef,
}: {
  value: string
  onChange: (v: string) => void
  /** Which of the two views to show. A change reconfigures the live editor's
   *  presentation compartment; it never rebuilds the state, so the document,
   *  the undo history and the attachment refcounts all survive it. */
  mode: MarkdownMode
  placeholder?: string
  className?: string
  /** Return true to consume the files (an image upload); false lets the
   *  event fall through to CodeMirror's own paste/drop handling. */
  onPasteFiles?: (files: File[]) => boolean
  /** The note/task the body belongs to, for resolving bare-filename image
   *  embeds and for holding their bytes while the editor is open. */
  getParent?: () => ImageUploadParent | undefined
  /** Reports which constructs the caret is inside, for the toolbar. */
  onActive?: (marks: Set<ActiveMark>) => void
  handleRef?: Ref<SourceEditorHandle>
}) {
  const host = useRef<HTMLDivElement>(null)
  const viewRef = useRef<EditorView | null>(null)
  // The emit path and the external-value effect are the two halves of one
  // loop: without this, every keystroke would come back through ``value``
  // as an "external" change and re-dispatch onto the document it came from.
  const lastEmitted = useRef(value)
  // The handlers are rebuilt on every render but the editor is built once,
  // so the extensions read them through refs and always see the live ones.
  const onChangeRef = useRef(onChange)
  const onPasteRef = useRef(onPasteFiles)
  const getParentRef = useRef(getParent)
  const onActiveRef = useRef(onActive)
  // The mode has to be readable from the extension builders, which run again
  // on the CRLF rebuild path below: a builder that closed over the mount-time
  // mode would silently drop the editor back to the default halfway through a
  // session, on a transition nobody asked for.
  const modeRef = useRef(mode)
  useEffect(() => {
    onChangeRef.current = onChange
    onPasteRef.current = onPasteFiles
    getParentRef.current = getParent
    onActiveRef.current = onActive
    modeRef.current = mode
  })

  useEffect(() => {
    const parent = host.current
    if (!parent) return
    const extensionsFor = (src: string) =>
      markdownSourceExtensions({
        src,
        placeholder,
        mode: modeRef.current,
        onChange: (v) => {
          lastEmitted.current = v
          onChangeRef.current(v)
        },
        onPasteFiles: (files) => onPasteRef.current?.(files) ?? false,
        getParent: () => getParentRef.current?.(),
        onActive: (marks) => onActiveRef.current?.(marks),
      })
    const view = new EditorView({
      state: EditorState.create({
        doc: lastEmitted.current,
        extensions: extensionsFor(lastEmitted.current),
      }),
      parent,
    })
    viewRef.current = view
    // Stamped from HERE and from the mode effect below, never from JSX: the
    // attribute has to mean "the editor's presentation compartment holds this
    // mode", and React writes a JSX attribute during the commit that PRECEDES
    // the effect doing the reconfigure. A browser test reading it would be
    // racing the thing it was waiting for.
    parent.setAttribute('data-md-mode', modeRef.current)
    // The update listener never fires for the initial state, so the toolbar
    // would start with nothing pressed until the first keystroke.
    onActiveRef.current?.(activeMarks(view.state))
    return () => {
      view.destroy()
      viewRef.current = null
    }
    // Built once for the editor's lifetime. ``placeholder`` is captured at
    // mount, like the textarea's was; the value flows through the effect
    // below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Reflect an EXTERNAL value change (a different part loaded, a conflict
  // reload, an accepted suggestion) onto the view, and only that: a value
  // equal to what this editor last emitted is its own content echoing back
  // through the parent, and re-dispatching it would fight the caret.
  useEffect(() => {
    const view = viewRef.current
    if (!view) return
    if (value === lastEmitted.current) return
    lastEmitted.current = value
    syncExternal(
      view,
      value,
      (src) =>
        markdownSourceExtensions({
          src,
          placeholder,
          mode: modeRef.current,
          onChange: (v) => {
            lastEmitted.current = v
            onChangeRef.current(v)
          },
          onPasteFiles: (files) => onPasteRef.current?.(files) ?? false,
          getParent: () => getParentRef.current?.(),
          onActive: (marks) => onActiveRef.current?.(marks),
        }),
      (rebuilt) => {
        lastEmitted.current = rebuilt
        onChangeRef.current(rebuilt)
      },
    )
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value])

  // Reflect a mode change onto the live editor. Guarded by what was last
  // APPLIED rather than by a mount flag: the state is built in the current
  // mode already, and reconfiguring it to the same value would dispatch a
  // transaction for nothing.
  const appliedMode = useRef(mode)
  useEffect(() => {
    const view = viewRef.current
    if (!view || appliedMode.current === mode) return
    appliedMode.current = mode
    setMarkdownMode(view, mode, { getParent: () => getParentRef.current?.() })
    host.current?.setAttribute('data-md-mode', mode)
  }, [mode])

  useImperativeHandle(
    handleRef,
    (): SourceEditorHandle => ({
      insert: (md: string) => {
        const view = viewRef.current
        if (!view) return
        const doc = view.state.sliceDoc()
        // Unfocused means there is no caret the user chose: CodeMirror's
        // selection would be position 0, and dropping an attachment
        // reference at the top of the note is not what anyone meant. Append
        // instead, on its own line, which is what the old textarea's
        // ref-less fallback did.
        const focused = view.hasFocus
        const from = focused ? view.state.selection.main.from : doc.length
        const to = focused ? view.state.selection.main.to : doc.length
        const lead = focused || !doc || doc.endsWith('\n') ? '' : '\n'
        const insert = lead + md
        view.dispatch({
          changes: { from, to, insert },
          selection: { anchor: from + insert.length },
          scrollIntoView: true,
        })
        view.focus()
      },
      scrollToFraction: (f: number) => {
        const view = viewRef.current
        if (!view) return
        const clamped = Math.max(0, Math.min(1, f))
        const el = view.scrollDOM
        el.scrollTop = clamped * (el.scrollHeight - el.clientHeight)
        host.current?.scrollIntoView({
          behavior: 'smooth',
          block: clamped <= 0 ? 'start' : clamped >= 1 ? 'end' : 'center',
        })
      },
      focus: () => viewRef.current?.focus(),
      run: (cmd) => {
        const view = viewRef.current
        return view ? cmd(view) : false
      },
      view: () => viewRef.current,
    }),
    [],
  )

  return <div ref={host} className={className} />
}
