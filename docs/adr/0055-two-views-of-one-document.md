# ADR-0055: Two views of one document, not two documents

Status: Accepted (2026-08-29)

Revises: the editor decision recorded in commit `0228012` ("the markdown IS the
document — tiptap is gone") and written into `docs/markdown-syntax.md`, which
removed the mode toggle along with the surface it switched to and left a single
hybrid view. The byte-exactness argument in that commit stands and is not
touched here; what is revised is the conclusion that one view had to follow
from it.

Relates to: `docs/markdown-syntax.md` (the user-facing statement of the same
thing), `web/test/markdown-corpus/` (the fixtures the guarantee is measured
against), ADR-0038 (entity chips, which survive the toggle because they sit
outside it).

## Context

There used to be two editing surfaces with a toggle: a ProseMirror rich editor
and a plain markdown textarea. The rich one held a *document model*, so saving
meant running a serializer, and that serializer was not the identity. Measured
across 82 constructs it re-flowed hard-wrapped paragraphs, rewrote `*` and `+`
bullets, dropped table alignment, escaped `[` and `_` with backslashes, and
corrupted YAML front matter, footnotes, a fence containing a ``` run, an escaped
`\|` inside a table cell and an image-only cell. Opening such a body was safe;
editing it once was not. That matters here beyond tidiness: bodies arrive from
an external editor over the REST API, and from an LLM over MCP, and both expect
to read back what they wrote.

`0228012` fixed it at the root — the editor's document became the markdown
string, read with `state.sliceDoc()`, with no serializer anywhere — and, in the
same move, deleted the toggle and replaced both surfaces with one hybrid view
in which markup receded on every line the caret was not on.

The hybrid is what this ADR is about. Clicking anywhere in a paragraph brought
back every `**`, every `[` and every `](url)` on that line at once, and the text
jumped. It read as neither a rendered document nor markdown, and there was no
longer any way to ask for either.

## Decision

**One document, two ways of showing it, and a toggle between them.**

The substrate does not change: one CodeMirror over the markdown string, no
serializer, `sliceDoc()` in both views. What the toggle switches is a
CodeMirror `Compartment` holding the presentation:

- **markdown** installs a monospace theme and no preview layers at all, so
  there is nothing in the configuration that could hide or replace a byte;
- **rendered** installs the preview layers and the reader's typography.

Switching dispatches a reconfigure and a re-assertion of the current selection.
It carries no `changes`, so it is not a document change, and the autosave —
which is string-equality gated — provably cannot fire on it.

**In the rendered view, markup recedes except on the construct the caret is
in**, not the line. A caret in a bold word shows that word's `**` and leaves the
rest of the paragraph rendered.

## Consequences

The byte-exactness guarantee is structural in both views rather than
maintained: there is one document and no serializer on either side, so the
toggle is a view setting and cannot be a data risk. The corpus test now runs
against a mounted editor in the rendered view, which is the first time the
inline layer is exercised against the fixtures at all.

Every escape hatch the hybrid had is kept, and the block ones are made
explicit: clicking a rendered table, diagram, formula or image gives its source
back, through the same selection-driven reveal rule both layers already read,
so there is no second reveal for them to disagree about.

The honest limits are in `docs/markdown-syntax.md` under "What the rendered
view does not do", where the people affected by them will find them.

## Alternatives rejected

**Hide the markup unconditionally in the rendered view.** This was the first
design, and it was measured rather than argued about, against the installed
parser. Three things fall out of it, none fixable by any policy that is a pure
function of the document. `x **bold*` genuinely parses as *italic* for one
keystroke while `x **bold**` is being typed, so the line shrinks and shifts
mid-word and a `*` the author never typed at that position is left standing.
Backspace next to an invisible delimiter deletes half a pair, and the orphaned
half then stops being hidden — the author asked to delete one character and
three appeared. And the two document positions either side of a zero-width
hidden run render at the same pixel with opposite typing semantics, decided by
motion history and observable nowhere. `EditorView.atomicRanges` does not
rescue any of this: for deletion it *extends* the change over the whole atom
rather than refusing it, and it is consulted only for cursor motion, pointer
selection and DOM-change mapping — never for the commands, which dispatch
directly. Recovering the affordances the hidden view destroys (a pending-mark
model for `B` with no selection, list continuation, a quote-aware Enter, a
link-destination popover, an image alt/src editor, a per-block reveal field)
amounts to rebuilding a rich-text input layer on top of a text editor, one
affordance at a time.

**Bring back ProseMirror with an identity-preserving serializer.** The corpus
is the argument against it. `bullet-markers.md`, `ordered-delimiters.md`,
`thematic-breaks.md`, `fences.md`, `hard-breaks.md` and `blank-line-runs.md`
each hold two spellings of one construct *inside a single file*, while a
markdown serializer's `bullet`, `bulletOrdered`, `rule` and `fence` are one
setting per document. And whether `[c]` needs escaping depends on a link
definition three lines away. A serializer that preserved all of this would have
to carry the original bytes for every node it did not touch, which is the
current design with more machinery in front of it.

**A contentEditable projection with a source map back to the markdown
offsets.** Byte-safe and rendering-perfect in principle, and the only option
that could hide markup completely. It forks every consumer of the selection
(annotations, chips, the toolbar, the browser suite's page object) and inherits
`beforeinput`, IME composition, drag and drop and undo mapping as things to
implement rather than to receive. Kept on the shelf for the day the rendered
view becomes the only view.
