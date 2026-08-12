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

## Verbatim bodies and the visual editor

A body is stored as the markdown its author wrote. Nothing on the way in
reformats it: MCP, the CLI and imports write the bytes they were given, and
the SPA reads them. **The visual editor is a view mode, not a storage
format** — it is the default one for every body, wherever the body came
from, and opening a note in it changes nothing on the server.

What *is* asymmetric is writing. The visual editor can only save what its
serializer produces, and that serialization is not the identity: it re-flows
a hard-wrapped paragraph onto one line, escapes `[` and `_` outside words,
and normalises table separators to `| --- |`. (The trailing newline is
preserved.) So the editor measures, once per body, whether the body is a
**fixed point** of its own round-trip, and when it is not it says so in a
notice: reading it costs nothing, the first edit made *there* saves the
normalised form. Content authored in the app is a fixed point by
construction, so the notice never appears on it.

Practical consequence for anything meant to stay byte-exact: edit it under
"Edit as Markdown", where what you type is what is stored.

Things that are NOT fixed points, hence carry the notice: hard-wrapped
paragraphs, display maths (`$$ … $$`), an underscore between non-ASCII
characters (`Φ_ℓ`), and any HTML other than `<sub>` / `<sup>`. Inline maths
(`$x_0$`), tables, lists, code fences, mermaid, attachment references,
mention chips and the trailing newline all round-trip unchanged.
