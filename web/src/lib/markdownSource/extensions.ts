import { EditorState, type Extension } from '@codemirror/state'
import { EditorView, keymap, placeholder as cmPlaceholder } from '@codemirror/view'
import { defaultKeymap, history, historyKeymap } from '@codemirror/commands'
import { markdown, markdownLanguage } from '@codemirror/lang-markdown'
import { HighlightStyle, syntaxHighlighting } from '@codemirror/language'
import { tags as t } from '@lezer/highlight'
import { lineSepFor } from './lineSep'

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
})

export function markdownSourceExtensions(opts: SourceOptions): Extension[] {
  const sep = lineSepFor(opts.src)
  return [
    ...(sep ? [EditorState.lineSeparator.of(sep)] : []),
    history(),
    keymap.of([...defaultKeymap, ...historyKeymap]),
    markdown({ base: markdownLanguage }),
    syntaxHighlighting(mdHighlight),
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
      if (!u.docChanged) return
      opts.onChange(u.state.sliceDoc())
    }),
  ]
}
