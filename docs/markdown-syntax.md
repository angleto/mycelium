# Markdown syntax supported in note and task bodies

Reachable as `help('markdown-syntax')`. Every body in Mycelium (note parts,
task descriptions, checklist items, comments) is markdown, rendered by one
renderer (`web/src/components/Markdown.tsx`) and edited by one editor
(`web/src/components/RichEditor.tsx`). This page says what the two agree on,
so nobody has to find out by trial and error.

## Baseline

CommonMark plus GitHub-flavored extensions: headings, emphasis, lists,
blockquotes, fenced and inline code, tables, task lists (`- [ ]` / `- [x]`),
strikethrough (`~~text~~`), and autolinked URLs written as explicit links.

## Maths

| you write | you get |
| --- | --- |
| `$x_0$`, `$T^t(n) = \dfrac{3^s n + c}{2^t}$` | inline formula (KaTeX) |
| `$$ … $$` | display formula, stacked fractions, sums with limits |
| `x<sub>0</sub>`, `2<sup>t</sup>` | subscript / superscript |
| `x₀`, `n²` | literal Unicode, always works, incomplete alphabet |

LaTeX via KaTeX is the only syntax that produces real formulae. `<sub>` and
`<sup>` are the cheap option for light notation inside prose, and they
round-trip through the visual editor.

Not supported, by decision: Pandoc's `^superscript^` and `~subscript~`. A
single `~` collides with GFM strikethrough, and the notation is niche.

## HTML

Only `<sub>` and `<sup>` are interpreted. Every other tag stays literal
text, in the renderer and in the editor alike. There is no raw-HTML mode:
the renderer never receives an HTML string it did not build itself, so a
body cannot inject markup.

## Mycelium-specific

| you write | you get |
| --- | --- |
| `` `91cf6aaa` `` | clickable chip for a task/note whose id starts with those hex digits (ADR-0038) |
| `[label](@task:<uuid>)`, `@note:`, `@tag:` | in-app link chip |
| `![name](/attachments/<id>/download)` | the attachment inline: image, audio, video, or text preview |
| `![alt](file.png)` | same, resolved by filename against the note's or task's own attachments |
| ` ```mermaid ` | live diagram, in the reader and in the editor |

## Verbatim bodies: the storage guarantee

A body is stored as the markdown its author wrote. Nothing on the way in
reformats it: MCP, the CLI, the REST API and imports write the bytes they
were given, and the SPA reads them. Leading indentation, tabs, runs of blank
lines, a two-space hard break and the trailing newline are all markdown, and
all survive.

One rule follows from the fact that a note is a list of **parts**, not a
single string. The flat body (`transcript` in the REST payload, what MCP
`get_note` returns) is the `\n\n` join of every part, and that join is not
invertible: a blank line inside a part reads exactly like a part boundary.
So the flat-body writers (`PATCH /notes/{id}` with `text`, MCP `update_note`,
`mycelium note edit`) accept a body they can express and refuse one they
cannot:

- a note with zero or one part: `text` replaces the body, as always;
- a note with several parts, `text` unchanged: no-op on the parts, so a
  read/modify-nothing/write-back round trip is the identity;
- a note with several parts, `text` changed: **refused** (422,
  `note.body.multipart`). Edit the part you mean through the note-parts
  surface (`PATCH /notes/{id}/parts/{part_id}`, MCP `update_note_part`,
  `mycelium note parts replace`), which knows which part changed.

Refusing rather than collapsing is deliberate: collapsing the parts into one
would cascade-delete every comment and suggestion anchored to the parts that
disappear.

## The editor

**The editor's document IS the markdown.** Reading it is `state.sliceDoc()`
and nothing else: there is no serializer between what you type and what is
stored, and therefore nothing that can be lossy. It never rewrites a byte you
did not type.

This is a change of substrate, not a setting. There used to be two surfaces,
a visual one and a source one, and the visual one could only save what its
markdown serializer produced. That serializer was not the identity: measured
across 82 constructs it re-flowed hard-wrapped paragraphs, rewrote `*` and `+`
bullets, dropped table alignment, escaped `[` and `_`, and outright corrupted
YAML front matter, footnotes, a fence containing a ``` run, an escaped `\|`
inside a table cell and an image-only cell. Opening such a body was safe;
editing it once was not, and a notice used to say so. Both the surface and the
notice are gone.

`web/test/markdown-corpus/` holds one fixture per construct that used to
break, and `pnpm test` asserts that every one of them comes back out of the
editor exactly as it went in. There is no allowlist of known-lossy cases: a
fixture that cannot round-trip is a bug. The corpus runs in the `web` CI job,
needs no backend, and takes about two seconds.

The one stated limit is line endings. A uniformly-LF body and a uniformly-CRLF
body are both exact. A body that MIXES the two displays correctly and writes
nothing when opened, but normalises to LF on the first edit: pinning CRLF for
such a body would make CodeMirror split on `\r\n` only, collapsing the whole
document into a single line with no block structure at all.

### It renders while you write

Markup recedes on every line the caret is not on: `## Titolo` shows as a
heading, `**grassetto**` as bold, `[label](url)` as an underlined label. Put
the caret on the line and the source comes straight back, so you are always
editing markdown, never a rendering of it. The whole layer is decorations; it
dispatches no document change, which is what the tests assert first.

Whole blocks get a real preview: a ` ```mermaid ` fence renders as its
diagram, a `$$ … $$` block as the typeset formula, a GFM table as a table with
its alignments, an `![alt](…)` embed as the picture, and a setext underline
folds into the heading above it. For a diagram and a formula the preview stays
visible *underneath* the source while you edit it, because writing either of
them blind is not writing them.

One construct deliberately keeps its source: an autolink (`<https://…>`),
which is all URL, so hiding the destination would leave an empty line with no
label to put in its place.

### The toolbar

Every formatting button is a transformation of the source: `B` inserts `**`,
`H2` puts `## ` on each selected line, the list buttons switch marker kind
instead of stacking one on top of another, and the link button escapes the
label it wraps. Undo and redo are the editor's own.

Two behaviours worth knowing. A button **refuses** rather than guessing when
its range crosses a blank line (one pair of `**` cannot wrap two paragraphs)
or lands inside a code fence or a table's delimiter row, where those
characters would not be markup. And a table's columns are re-aligned only when
you press the re-align button: nothing reformats a table you are merely
editing, because rewriting bytes nobody typed is the thing this editor exists
not to do.

Tab and Shift-Tab move between table cells, and do nothing anywhere else, so
the key keeps its usual meaning outside a table.

### Typeaheads and pasting

`@` followed by a title searches this workspace's tasks, notes and tags and
inserts `[label](@kind:uuid)`. `[[` followed by a title, or by an 8-character
id prefix, inserts the backticked chip (`` `91cf6aaa` ``). Both escape the
label they wrap.

Pasting markdown source works: the document IS the text, so a reference, a
table or a whole document lands as itself. (The visual editor took a pasted
`![name](/attachments/…)` as literal text and escaped it on the way out, so
the stored body held `!\[name\](…)` and readers saw the characters instead of
the image.) Pasting a URL over a selection wraps it as a link, and pasting or
dropping an image file uploads it.

### Comments and suggestions

Their anchor is the markdown SOURCE: select `**importante**` and that is what
gets quoted, struck and spliced. A selection is grown so it never covers one
delimiter of a pair without the other, since half a delimiter is markup with
nothing to close it; a selection covering neither is left alone, because
commenting on just the word inside a bold run is the ordinary thing to want.

Annotations written by the retired visual editor quoted a RENDERED projection
instead. Migration 0099 converted every one it could; the rest stay listed in
the panel, unpainted, rather than being drawn over a passage nobody chose.

One limit: an annotation whose passage sits inside a block currently shown as
a widget (a rendered table, a diagram) is not highlighted until you put the
caret in that block and its source comes back. The annotation is still there
and still accepts.
