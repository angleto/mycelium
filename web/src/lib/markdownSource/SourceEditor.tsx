import { useEffect, useImperativeHandle, useRef, type Ref } from 'react'
import { EditorState, Transaction, type Extension } from '@codemirror/state'
import { EditorView } from '@codemirror/view'
import { lineSepFor } from './lineSep'
import { markdownSourceExtensions } from './extensions'
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
}

/**
 * Push an externally-changed ``next`` onto a live view as the smallest
 * change that produces it.
 *
 * Two things are load-bearing here.
 *
 * ``Transaction.addToHistory.of(false)``: an external value is not something
 * the user did, so it must not enter the undo stack. Without it, accepting a
 * suggestion (which reloads the body from the server) becomes undoable, and
 * one Cmd+Z would write the pre-accept body back over the accepted one while
 * the annotation stayed ``accepted``.
 *
 * The ``setState`` branch: ``lineSeparator`` is read once, when the state is
 * created, and later inserts split on the CURRENT ``state.lineBreak``. It
 * cannot be swapped by reconfiguring. So when the incoming body wants a
 * different separator than the live one, the state is rebuilt rather than
 * patched.
 */
function syncExternal(
  view: EditorView,
  next: string,
  extensionsFor: (src: string) => Extension[],
): void {
  const cur = view.state.sliceDoc()
  if (cur === next) return
  const wanted = lineSepFor(next) ?? '\n'
  const live = view.state.lineBreak
  if (wanted !== live) {
    view.setState(EditorState.create({ doc: next, extensions: extensionsFor(next) }))
    return
  }
  // Common prefix / suffix, so a one-character edit dispatches a
  // one-character change and the selection of anyone else looking at this
  // document maps through it instead of being reset.
  let a = 0
  const max = Math.min(cur.length, next.length)
  while (a < max && cur[a] === next[a]) a += 1
  let b = 0
  while (b < max - a && cur[cur.length - 1 - b] === next[next.length - 1 - b]) b += 1
  view.dispatch({
    changes: { from: a, to: cur.length - b, insert: next.slice(a, next.length - b) },
    annotations: Transaction.addToHistory.of(false),
  })
}

export function SourceEditor({
  value,
  onChange,
  placeholder,
  className,
  onPasteFiles,
  getParent,
  handleRef,
}: {
  value: string
  onChange: (v: string) => void
  placeholder?: string
  className?: string
  /** Return true to consume the files (an image upload); false lets the
   *  event fall through to CodeMirror's own paste/drop handling. */
  onPasteFiles?: (files: File[]) => boolean
  /** The note/task the body belongs to, for resolving bare-filename image
   *  embeds and for holding their bytes while the editor is open. */
  getParent?: () => ImageUploadParent | undefined
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
  useEffect(() => {
    onChangeRef.current = onChange
    onPasteRef.current = onPasteFiles
    getParentRef.current = getParent
  })

  useEffect(() => {
    const parent = host.current
    if (!parent) return
    const extensionsFor = (src: string) =>
      markdownSourceExtensions({
        src,
        placeholder,
        onChange: (v) => {
          lastEmitted.current = v
          onChangeRef.current(v)
        },
        onPasteFiles: (files) => onPasteRef.current?.(files) ?? false,
        getParent: () => getParentRef.current?.(),
      })
    const view = new EditorView({
      state: EditorState.create({
        doc: lastEmitted.current,
        extensions: extensionsFor(lastEmitted.current),
      }),
      parent,
    })
    viewRef.current = view
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
    syncExternal(view, value, (src) =>
      markdownSourceExtensions({
        src,
        placeholder,
        onChange: (v) => {
          lastEmitted.current = v
          onChangeRef.current(v)
        },
        onPasteFiles: (files) => onPasteRef.current?.(files) ?? false,
        getParent: () => getParentRef.current?.(),
      }),
    )
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value])

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
    }),
    [],
  )

  return <div ref={host} className={className} />
}
