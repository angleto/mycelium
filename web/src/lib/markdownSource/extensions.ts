import { EditorState, type Extension } from '@codemirror/state'
import { EditorView, keymap, placeholder as cmPlaceholder } from '@codemirror/view'
import { defaultKeymap, history, historyKeymap } from '@codemirror/commands'
import { commonmarkLanguage, markdown } from '@codemirror/lang-markdown'
import { GFM } from '@lezer/markdown'
import { HighlightStyle, syntaxHighlighting } from '@codemirror/language'
import { tags as t } from '@lezer/highlight'
import { lineSepFor } from './lineSep'
import { attachmentRetain } from './attachmentRetain'
import { tableKeymap } from './tableCommands'
import { markdownCompletion } from './completion'
import { activeMarks, type ActiveMark } from './commands'
import { annotationLayer } from './annotationLayer'
import { entityChips } from './entityChips'
import { notifySelection } from '../annotationSurface'
import type { ImageUploadParent } from '../imageUpload'
import { blockPreview } from './blockPreview'
import { livePreview } from './livePreview'

// The CodeMirror configuration of the markdown source surface, kept apart
// from the React shell in SourceEditor.tsx so the contract this file encodes
// -- what the editor reads, what it emits, and when -- can be asserted
// directly against an EditorState/EditorView, with no component to mount.

export type SourceOptions = {
  /** The document the state is being built for. Only its LINE ENDINGS are
   *  read here (see lineSepFor); the text itself is passed to
   *  ``EditorState.create`` by the caller. */
  src: string
  placeholder?: string
  onChange: (v: string) => void
  /** Return true to consume a paste/drop of files (an image upload); false
   *  lets the event fall through to CodeMirror's own handling. */
  onPasteFiles?: (files: File[]) => boolean
  /** The note or task the body belongs to, read LIVE: it resolves a
   *  bare-filename image embed against that parent's own attachments, and a
   *  brand-new note has no id yet when its editor is built. */
  getParent?: () => ImageUploadParent | undefined
  /** The constructs the caret is inside, for the toolbar's pressed state.
   *  Fired on every selection or document change. */
  onActive?: (marks: Set<ActiveMark>) => void
}

// Syntax highlighting for the source. Deliberately restrained: this is a
// writing surface, not a code viewer, so it marks structure (headings,
// emphasis, links, code) and leaves prose alone. Colours come from the app's
// CSS variables so light/dark follow the theme with no second palette.
const mdHighlight = HighlightStyle.define([
  { tag: t.heading, color: 'var(--text)', fontWeight: '700' },
  { tag: t.strong, fontWeight: '700' },
  { tag: t.emphasis, fontStyle: 'italic' },
  { tag: t.strikethrough, textDecoration: 'line-through' },
  { tag: [t.link, t.url], color: 'var(--accent)' },
  { tag: [t.monospace, t.literal], color: 'var(--accent)' },
  { tag: t.quote, color: 'var(--muted)', fontStyle: 'italic' },
  { tag: t.list, color: 'var(--accent)' },
  // The delimiters themselves (``##``, ``**``, ``[``) stay visible but
  // recede, so the source reads as prose without hiding what it is.
  { tag: [t.processingInstruction, t.punctuation], color: 'var(--muted)' },
  { tag: t.contentSeparator, color: 'var(--muted)' },
])

// Structural theming lives here rather than in index.css because CodeMirror
// injects its base theme through StyleModule, and the load order against the
// app's stylesheet is not guaranteed. A theme extension always wins.
const baseTheme = EditorView.theme({
  '&': {
    color: 'var(--text)',
    backgroundColor: 'transparent',
    height: '100%',
    fontSize: 'inherit',
  },
  '&.cm-focused': { outline: 'none' },
  '.cm-scroller': {
    fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
    lineHeight: '1.55',
    overflow: 'auto',
  },
  '.cm-content': {
    padding: '0.55rem 0.7rem',
    caretColor: 'var(--text)',
  },
  '.cm-line': { padding: '0' },
  '.cm-cursor, .cm-dropCursor': { borderLeftColor: 'var(--text)' },
  '&.cm-focused .cm-selectionBackground, .cm-selectionBackground, ::selection': {
    backgroundColor: 'color-mix(in srgb, var(--accent) 28%, transparent)',
  },
  '.cm-placeholder': { color: 'var(--muted)' },

  // Live-preview structure. These are LINE decorations, so they style the
  // line the construct occupies without the document knowing anything about
  // it. Sizes are relative so the whole surface still scales with the host's
  // font-size (the notes editor and the checklist editor are not the same
  // size).
  '.cm-md-h1': { fontSize: '1.5em', fontWeight: '700', lineHeight: '1.3' },
  '.cm-md-h2': { fontSize: '1.3em', fontWeight: '700', lineHeight: '1.3' },
  '.cm-md-h3': { fontSize: '1.15em', fontWeight: '700' },
  '.cm-md-h4': { fontWeight: '700' },
  '.cm-md-h5': { fontWeight: '700', color: 'var(--muted)' },
  '.cm-md-h6': { fontWeight: '700', color: 'var(--muted)' },
  '.cm-md-quote': {
    borderLeft: '3px solid var(--border)',
    paddingLeft: '0.6em',
    color: 'var(--muted)',
    fontStyle: 'italic',
  },
  '.cm-md-code': {
    backgroundColor: 'color-mix(in srgb, var(--text) 6%, transparent)',
  },
  '.cm-md-hr': {
    color: 'var(--muted)',
    borderBottom: '1px solid var(--border)',
  },
  '.cm-md-linklabel': {
    color: 'var(--accent)',
    textDecoration: 'underline',
    textUnderlineOffset: '2px',
  },

  // Block widgets (blockPreview.ts). They stand where several source lines
  // would be, so they own their vertical rhythm; ``user-select: none`` keeps
  // a drag across one from producing a selection that has no counterpart in
  // the document.
  '.cm-md-widget': {
    margin: '0.4em 0',
    userSelect: 'none',
  },
  // An image embed is replaced INLINE (it can sit mid-paragraph), so the
  // widget is an inline box rather than a block one.
  '.cm-md-widget-inline': { display: 'inline-block', verticalAlign: 'middle' },
  '.cm-md-mermaid': { display: 'flex', justifyContent: 'center' },
  '.cm-md-mermaid svg': { maxWidth: '100%', height: 'auto' },
  '.cm-md-math': { overflowX: 'auto', textAlign: 'center' },
  '.cm-md-math--error': {
    fontFamily: 'inherit',
    color: 'var(--muted)',
    textAlign: 'left',
  },
  // The table scrolls inside its own box rather than widening the editor:
  // a wide table must never make the whole writing surface scroll sideways.
  '.cm-md-table': { overflowX: 'auto' },
  '.cm-md-table table': {
    borderCollapse: 'collapse',
    fontSize: '0.95em',
  },
  '.cm-md-table th, .cm-md-table td': {
    border: '1px solid var(--border)',
    padding: '0.25em 0.6em',
    textAlign: 'left',
  },
  '.cm-md-table th': {
    fontWeight: '700',
    backgroundColor: 'color-mix(in srgb, var(--text) 5%, transparent)',
  },
})

export function markdownSourceExtensions(opts: SourceOptions): Extension[] {
  const sep = lineSepFor(opts.src)
  return [
    ...(sep ? [EditorState.lineSeparator.of(sep)] : []),
    history(),
    keymap.of([...defaultKeymap, ...historyKeymap]),
    // CommonMark + GFM, and NOTHING else. ``markdownLanguage`` (the obvious
    // choice) is commonmark + GFM + Subscript + Superscript + Emoji, and the
    // last three are syntax this app does not have: docs/markdown-syntax.md
    // rules out Pandoc's ``^sup^`` / ``~sub~`` by decision (a single ``~``
    // collides with GFM strikethrough), and the reader renders them as
    // literal text. An editor that highlighted them as markup would be
    // promising a rendering that never arrives.
    //
    // ``completeHTMLTags: false`` for the same reason: only ``<sub>`` and
    // ``<sup>`` are interpreted anywhere in Mycelium, so completing arbitrary
    // HTML tags would be an invitation to write text that stays literal.
    markdown({ base: commonmarkLanguage, extensions: [GFM], completeHTMLTags: false }),
    syntaxHighlighting(mdHighlight),
    // Markup recedes on the lines the caret is not on. Presentational only:
    // it never dispatches a document change, so the bytes are the same with
    // it as without it.
    livePreview({ getParent: opts.getParent }),
    // Block-level previews (mermaid, `$$`, tables, setext folding). A state
    // field, not a plugin: these replace ranges that span line breaks and
    // determine their own height.
    blockPreview(),
    // Hold the bytes of every embedded attachment for the editor's lifetime,
    // so a widget destroyed by the caret moving onto its line does not take
    // the refcount to zero and throw the image away.
    attachmentRetain(opts.getParent ?? (() => undefined)),
    // Tab moves between table cells and does nothing elsewhere, so the key
    // keeps its accessibility meaning outside a table.
    tableKeymap(),
    // `@` and `[[` typeaheads, inserting the same markdown the retired
    // surface did, so a body reads the same whoever wrote it.
    markdownCompletion(),
    // Comment / suggestion marks. A bare state field: the annotations
    // themselves live in React state and arrive through a StateEffect.
    annotationLayer(),
    // Clickable chips for a backticked UUID prefix (ADR-0038). Routing is
    // the global AppShell interceptor, keyed on data-entity-prefix.
    entityChips(),
    EditorView.lineWrapping,
    baseTheme,
    ...(opts.placeholder ? [cmPlaceholder(opts.placeholder)] : []),
    EditorView.domEventHandlers({
      paste(event) {
        const items = event.clipboardData?.items
        if (!items || !opts.onPasteFiles) return false
        const files: File[] = []
        for (let i = 0; i < items.length; i += 1) {
          if (items[i].kind !== 'file') continue
          const f = items[i].getAsFile()
          if (f) files.push(f)
        }
        if (!files.length) return false
        // The handler decides whether it wants them (only images are
        // uploadable); anything it declines falls through to CodeMirror's
        // own paste, which is what pastes the text half of a mixed payload.
        return opts.onPasteFiles(files)
      },
      drop(event) {
        const files = event.dataTransfer?.files
        if (!files || !files.length || !opts.onPasteFiles) return false
        return opts.onPasteFiles(Array.from(files))
      },
    }),
    EditorView.updateListener.of((u) => {
      if (u.docChanged) opts.onChange(u.state.sliceDoc())
      if (u.docChanged || u.selectionSet) opts.onActive?.(activeMarks(u.state))
      // A CodeMirror extension is fixed at state creation, so the annotation
      // UI -- which mounts after the editor -- cannot add its own listener.
      // One listener here, fanned out through the surface registry.
      if (u.docChanged || u.selectionSet) notifySelection(u.view)
    }),
  ]
}
