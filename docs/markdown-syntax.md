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

## The visual editor

**The visual editor is a view mode, not a storage format** — it is the
default one for every body, wherever the body came from, and opening a note
in it changes nothing on the server.

Writing is the asymmetric half, today. The current editor (tiptap) can only
save what its markdown serializer produces, and that serialization is not the
identity, so a body written outside the app is normalised the first time it is
edited *there*. The editor measures this once per body and says so in a
notice; content authored in the app is a fixed point by construction, so the
notice never appears on it.

The measured list is longer than this page used to claim. Beyond hard-wrapped
paragraphs, display maths (`$$ … $$`), an underscore between non-ASCII
characters (`Φ_ℓ`) and HTML other than `<sub>` / `<sup>`, the round trip also
rewrites: setext headings to ATX, `*` and `+` bullets to `-`, `1)` to `1.`,
`~~~` fences and indented code to ```` ``` ````, padded and **aligned** table
separators to `| --- |`, tight task lists to loose ones, reference links to
inline ones, and `[`, `*`, `_` to escaped forms. A few constructs are not
merely reformatted but corrupted: YAML front matter, footnotes, a fence
containing a ```` ``` ```` run, an escaped `\|` inside a table cell, and a
table cell holding only an image.

Practical consequence for anything meant to stay byte-exact: edit it under
"Edit as Markdown", where what you type is what is stored.

This is being fixed by changing what the editor's document *is*: the markdown
source itself, with rich rendering as a display layer over it, so there is no
serializer to be lossy. This section will shrink to "the editor stores what
you typed" when that lands.
