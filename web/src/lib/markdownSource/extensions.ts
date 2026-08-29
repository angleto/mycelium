import { EditorState, type Extension } from '@codemirror/state'
import { EditorView, keymap, placeholder as cmPlaceholder } from '@codemirror/view'
import { defaultKeymap, history, historyKeymap } from '@codemirror/commands'
import { commonmarkLanguage, markdown } from '@codemirror/lang-markdown'
import { GFM } from '@lezer/markdown'
import { lineSepFor } from './lineSep'
import { attachmentRetain } from './attachmentRetain'
import { tableKeymap } from './tableCommands'
import { markdownCompletion } from './completion'
import { activeMarks, type ActiveMark } from './commands'
import { annotationLayer } from './annotationLayer'
import { entityChips } from './entityChips'
import { notifySelection } from '../annotationSurface'
import type { ImageUploadParent } from '../imageUpload'
import { DEFAULT_MODE, markdownModeCompartment, presentationFor, type MarkdownMode } from './mode'
import { baseTheme } from './theme'

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
  /** Which of the two views this state starts in. Switched afterwards with
   *  ``setMarkdownMode``, which reconfigures the compartment below rather
   *  than rebuilding the state. */
  mode?: MarkdownMode
}

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
    // The presentation: which preview layers exist, and which typography.
    // Purely decorative in both configurations -- neither dispatches a
    // document change, so the bytes are the same in either, which is what
    // makes the toggle a view setting rather than a data decision.
    //
    // The slot MATTERS. Decoration rank is extension order, and the layers
    // have to outrank annotationLayer and entityChips below: a hidden `**`
    // has to nest INSIDE a comment highlight, or one annotation would be
    // painted as three fragments with the amber background broken at every
    // delimiter.
    markdownModeCompartment.of(presentationFor(opts.mode ?? DEFAULT_MODE, {
      getParent: opts.getParent,
    })),
    // Hold the bytes of every embedded attachment for the editor's lifetime,
    // so a widget destroyed by the caret moving onto its line does not take
    // the refcount to zero and throw the image away. OUTSIDE the compartment,
    // deliberately: making it mode-aware would release every attachment on a
    // switch to source mode and refetch them all on the way back.
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
