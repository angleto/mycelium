import type { Extension } from '@codemirror/state'
import { EditorView } from '@codemirror/view'
import { HighlightStyle, syntaxHighlighting } from '@codemirror/language'
import { tags as t } from '@lezer/highlight'

// How the markdown surface LOOKS, in each of its two modes.
//
// Kept apart from extensions.ts because the two mode themes go inside the
// presentation compartment (mode.ts) while the base one stays outside it, and
// a module that both of those import cannot be the one that assembles them.
//
// Structural theming lives here rather than in index.css because CodeMirror
// injects its base theme through StyleModule, and the load order against the
// app's stylesheet is not guaranteed. A theme extension always wins.

// Syntax highlighting for the source. Deliberately restrained: this is a
// writing surface, not a code viewer, so it marks structure (headings,
// emphasis, links, code) and leaves prose alone. Colours come from the app's
// CSS variables so light/dark follow the theme with no second palette.
//
// Two styles, because they disagree about exactly one tag. Highlighting is a
// MARK on the text inside a construct, so it beats a LINE class on
// specificity: a `t.heading` rule here would override the serif face, the 600
// weight and the `--text-h` colour that the rendered view's `.cm-md-h*` gives
// a heading to match the reader. So the rendered view leaves headings to the
// line class, and the markdown view -- which has no line classes at all --
// keeps them here, where they are the only thing making a heading look like
// one.
const COMMON_TAGS = [
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
]

const sourceHighlighting = syntaxHighlighting(
  HighlightStyle.define([
    { tag: t.heading, color: 'var(--text)', fontWeight: '700' },
    ...COMMON_TAGS,
  ]),
)

const visualHighlighting = syntaxHighlighting(HighlightStyle.define(COMMON_TAGS))

/** Everything both modes share: the box, the caret, the selection. Outside
 *  the compartment, so a mode switch cannot repaint it. Typography is
 *  deliberately absent -- it is exactly what the two modes disagree about. */
export const baseTheme = EditorView.theme({
  '&': {
    color: 'var(--text)',
    backgroundColor: 'transparent',
    height: '100%',
  },
  '&.cm-focused': { outline: 'none' },
  '.cm-scroller': {
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

/** Source mode: a text editor. Monospace, and nothing else -- every
 *  ``cm-md-*`` rule belongs to the rendered view, and in this mode no layer
 *  emits those classes at all. */
const sourceFaces = EditorView.theme({
  '.cm-scroller': {
    fontFamily: 'var(--mono)',
    fontSize: '0.85rem',
  },
})

export const sourceTheme: Extension = [sourceFaces, sourceHighlighting]

/**
 * Visual mode: what the reader looks like.
 *
 * Proportional body at the reader's own size, serif headings per the brand
 * guidelines (``--font-display``, 600, -0.01em, the same rule index.css gives
 * every h1-h3), and monospace restored wherever the markdown says code. Sizes
 * are relative so the whole surface still scales with its host: the notes
 * editor and the checklist editor are not the same size.
 *
 * Every value is a token. A literal colour here would be a defect in both
 * palettes at once, since only the tokens flip for dark mode.
 */
const visualFaces = EditorView.theme({
  '.cm-scroller': {
    fontFamily: 'var(--sans)',
    fontSize: '0.92rem',
  },

  // Live-preview structure. These are LINE decorations, so they style the
  // line the construct occupies without the document knowing anything about
  // it.
  '.cm-md-h1, .cm-md-h2, .cm-md-h3': {
    color: 'var(--text-h)',
    fontFamily: 'var(--font-display)',
    fontWeight: '600',
    letterSpacing: '-0.01em',
    lineHeight: '1.3',
  },
  '.cm-md-h1': { fontSize: '1.5em' },
  '.cm-md-h2': { fontSize: '1.15em' },
  '.cm-md-h3': { fontSize: '1.05em' },
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
    fontFamily: 'var(--mono)',
    fontSize: '0.9em',
  },
  // The content of an inline code span, which would otherwise go proportional
  // with the rest of the prose. A mark, not a replacement, so it nests inside
  // an annotation highlight rather than cutting it in two.
  '.cm-md-inlinecode': {
    fontFamily: 'var(--mono)',
    fontSize: '0.9em',
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
  // the document. ``cursor: text`` because clicking one brings its source
  // back, and an affordance nobody can see is not one.
  '.cm-md-widget': {
    margin: '0.4em 0',
    userSelect: 'none',
    cursor: 'text',
  },
  // An image embed is replaced INLINE (it can sit mid-paragraph), so the
  // widget is an inline box rather than a block one.
  '.cm-md-widget-inline': {
    display: 'inline-block',
    verticalAlign: 'middle',
    cursor: 'text',
  },
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

export const visualTheme: Extension = [visualFaces, visualHighlighting]
