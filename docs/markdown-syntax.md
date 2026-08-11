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

The visual editor stores what its serializer produces, and that
serialization is not the identity: it re-flows a hard-wrapped paragraph
onto one line, escapes `[` and `_` outside words, normalises table
separators to `| --- |`, and drops the trailing newline.

So the editor measures, once per body, whether that body is a **fixed
point** of its own round-trip. If it is not, the visual surface is withheld
and the body is shown as source, byte for byte, with a notice. Content
authored in the app is a fixed point by construction, so this is invisible
there; a file uploaded verbatim (MCP, CLI, an import) opens as source and
stays exactly as uploaded. Switching to the visual editor is possible and
explicit, and it rewrites the body in normalised form.

Practical consequence for anything meant to stay byte-exact: write it, or
upload it, and leave it in source mode.

Things that are NOT fixed points, so they open as source: hard-wrapped
paragraphs, a trailing newline, display maths (`$$ … $$`), an underscore
between non-ASCII characters (`Φ_ℓ`), and any HTML other than `<sub>` /
`<sup>`. Inline maths (`$x_0$`), tables, lists, code fences, mermaid,
attachment references and mention chips all round-trip unchanged.
