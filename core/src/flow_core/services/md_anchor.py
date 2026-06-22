"""Markdown-aware anchor resolution for inline suggestions.

A suggestion's anchor (``original_text`` + W3C ``prefix``/``suffix``) is
captured by the SPA from the editor's *rendered* text
(``doc.textBetween(from, to, ' ')``): markdown syntax stripped, links
reduced to their text, images/math contributing nothing, block
boundaries joined by a single space. The document body, however, is
stored as *markdown source*. Splicing the rendered quote into the source
directly (the old ``str.find``) fails the moment they differ — inline
formatting (``**bold**``), links, math, or a selection spanning blocks.

This module resolves the anchor in the SAME rendered domain the editor
captured it in, then maps the located span back to *source* offsets via a
per-character source map, and validates the splice by re-rendering: the
candidate body must render to exactly ``head + render(proposed) + tail``.
That equality makes corruption structurally impossible — either a
faithful splice or a decline (STALE). One authoritative path, shared by
the web, CLI and MCP accept flows, with no persisted character offsets
(the map is recomputed from the live body every time, so the anchor
survives prior edits and only goes stale when the rendered quote is no
longer uniquely locatable).

The renderer mirrors the editor's markdown-it configuration (CommonMark +
GFM strikethrough/table + the project's ``$...$`` / ``$$...$$`` math
rules, linkify/typographer OFF). Constructs this module does not model
exactly degrade to STALE (a safe no-op), never to a corrupting splice.
"""

from __future__ import annotations

from dataclasses import dataclass

from markdown_it import MarkdownIt
from markdown_it.rules_block import StateBlock
from markdown_it.rules_inline import StateInline
from markdown_it.token import Token


# --------------------------------------------------------------------------
# markdown-it instance mirroring the editor (web/src/components/RichEditor.tsx
# + MarkdownMath.ts): CommonMark + strikethrough + table + inline/block math,
# linkify + typographer OFF (they would substitute characters and break the
# verbatim source mapping).
# --------------------------------------------------------------------------
def _math_inline(state: StateInline, silent: bool) -> bool:
    """Port of MarkdownMath.ts inlineMathRule: ``$...$`` on one line, with
    the currency heuristics (no whitespace adjacency, no digit right after
    the closing ``$``)."""
    src = state.src
    start = state.pos
    maximum = state.posMax
    if start >= maximum or src[start] != "$":
        return False
    nxt = src[start + 1] if start + 1 < maximum else ""
    if nxt in ("$", " ", "\t", "\n", ""):
        return False
    pos = start + 1
    found = -1
    while pos < maximum:
        c = src[pos]
        if c == "\\":
            pos += 2
            continue
        if c == "\n":
            return False
        if c == "$":
            found = pos
            break
        pos += 1
    if found < 0:
        return False
    prev = src[found - 1]
    if prev in (" ", "\t"):
        return False
    after = src[found + 1] if found + 1 < maximum else ""
    if after and after.isdigit():
        return False
    if not silent:
        token = state.push("math_inline", "span", 0)
        token.markup = "$"
        token.content = src[start + 1 : found]
    state.pos = found + 1
    return True


def _math_block(state: StateBlock, start_line: int, end_line: int, silent: bool) -> bool:
    """Port of MarkdownMath.ts blockMathRule: ``$$...$$`` (single or
    multi-line)."""
    pos = state.bMarks[start_line] + state.tShift[start_line]
    maximum = state.eMarks[start_line]
    if pos + 2 > maximum:
        return False
    if state.src[pos : pos + 2] != "$$":
        return False
    first_line = state.src[pos + 2 : maximum]
    last_line = start_line
    content = ""
    if first_line.rstrip().endswith("$$"):
        content = first_line.rstrip()[:-2].strip()
    else:
        found = False
        line = start_line + 1
        while line < end_line:
            pos = state.bMarks[line] + state.tShift[line]
            maximum = state.eMarks[line]
            text = state.src[pos:maximum].rstrip()
            if text.endswith("$$"):
                last_line = line
                head = first_line.strip()
                tail = text[:-2].rstrip()
                middle = [
                    state.src[state.bMarks[m] + state.tShift[m] : state.eMarks[m]]
                    for m in range(start_line + 1, line)
                ]
                parts = [p for p in (head, "\n".join(middle) if middle else "", tail) if p]
                content = "\n".join(parts)
                found = True
                break
            line += 1
        if not found:
            return False
    if silent:
        return True
    token = state.push("math_block", "div", 0)
    token.block = True
    token.markup = "$$"
    token.content = content
    token.map = [start_line, last_line + 1]
    state.line = last_line + 1
    return True


def _build_md() -> MarkdownIt:
    md = MarkdownIt("commonmark").enable("strikethrough").enable("table")
    md.inline.ruler.after("escape", "math_inline", _math_inline)
    md.block.ruler.before(
        "fence",
        "math_block",
        _math_block,
        {"alt": ["paragraph", "reference", "blockquote", "list"]},
    )
    return md


_MD = _build_md()


# --------------------------------------------------------------------------
# line-ending normalisation (CRLF/CR -> LF) with a map back to the original
# offsets, so the splice preserves the document's untouched line endings.
# --------------------------------------------------------------------------
def _normalize(body: str) -> tuple[str, list[int]]:
    norm: list[str] = []
    omap: list[int] = []  # omap[i] = original offset of norm[i]
    i = 0
    n = len(body)
    while i < n:
        c = body[i]
        if c == "\r":
            norm.append("\n")
            omap.append(i)
            i += 2 if i + 1 < n and body[i + 1] == "\n" else 1
        else:
            norm.append(c)
            omap.append(i)
            i += 1
    omap.append(n)  # sentinel: a span end at len(norm) maps to len(body)
    return "".join(norm), omap


def _line_offsets(body: str) -> list[int]:
    offs = [0]
    for i, ch in enumerate(body):
        if ch == "\n":
            offs.append(i + 1)
    return offs


# Per-character source map entry: the rendered char and the [start, end)
# source span of its OWN bytes (the "inner edge": end is just past the
# char's last source byte, never past a following delimiter).
@dataclass(frozen=True)
class _Char:
    ch: str
    src_start: int
    src_end: int


# Source spans of an inline run's paired delimiters: the opening
# (``open_start``..``open_end``, e.g. ``**`` or ``[``) and the closing
# (``close_start``..``close_end``, e.g. ``**`` or ``](url)``). Used to detect
# and repair a splice span that straddles a run edge.
@dataclass(frozen=True)
class _Region:
    open_start: int
    open_end: int
    close_start: int
    close_end: int


# Mark/atom token types and how much source they consume; handled in
# _render_inline below.
_MARK_OPEN_CLOSE = {
    "strong_open",
    "strong_close",
    "em_open",
    "em_close",
    "s_open",
    "s_close",
}


def _render_inline(
    content: str, children: list[Token], base: int
) -> tuple[list[_Char], list[_Region]]:
    """Render one block's inline ``content`` to ``_Char`` cells, mirroring
    ProseMirror textBetween: marks contribute only their text, links only
    their link-text, images/math nothing, softbreak a single space,
    hardbreak nothing. ``base`` is the source offset of ``content`` within
    the body. Where the cursor cannot be tracked exactly the cells drift;
    the re-render gate in resolve_anchor turns any drift into a safe STALE,
    never a corrupting splice.

    Also returns the source spans of each inline RUN's paired delimiters
    (``**``/``*``/``~~`` for marks, ``[`` + ``](url)`` for links, the fence
    backticks for inline code), so resolve_anchor can detect a selection that
    straddles a run edge (one delimiter inside the splice span, its partner
    outside) and extend the span to swallow the whole run rather than leave
    an orphaned delimiter that would corrupt the body."""
    out: list[_Char] = []
    regions: list[_Region] = []
    stack: list[tuple[int, int]] = []  # open (src_start, src_end) for marks/links
    c = 0  # cursor into content
    n = len(content)

    def skip(run: str) -> None:
        nonlocal c
        if content[c : c + len(run)] == run:
            c += len(run)
        else:
            idx = content.find(run, c)
            if idx >= 0:
                c = idx + len(run)

    for child in children:
        t = child.type
        if t == "text":
            for char in child.content:
                if c < n and content[c] == "\\" and c + 1 < n and content[c + 1] == char:
                    out.append(_Char(char, base + c, base + c + 2))
                    c += 2
                elif c < n and content[c] == char:
                    out.append(_Char(char, base + c, base + c + 1))
                    c += 1
                else:
                    # Drift (entity decode, smart punct): best effort.
                    out.append(_Char(char, base + c, base + min(c + 1, n)))
                    c = min(c + 1, n)
        elif t in _MARK_OPEN_CLOSE:
            mk = child.markup or ""
            if t.endswith("_open"):
                o_start = base + c
                skip(mk)
                stack.append((o_start, base + c))
            else:
                cl_start = base + c
                skip(mk)
                if stack:
                    o0, o1 = stack.pop()
                    regions.append(_Region(o0, o1, cl_start, base + c))
        elif t == "code_inline":
            mk = child.markup or "`"
            o_start = base + c
            skip(mk)
            o_end = base + c
            close = content.find(mk, c)
            region_end = close if close >= 0 else n
            span = max(1, region_end - c)
            for k, char in enumerate(child.content):
                off = c + min(k, span - 1)
                out.append(_Char(char, base + off, base + off + 1))
            if close >= 0:
                regions.append(_Region(o_start, o_end, base + close, base + close + len(mk)))
            c = (close + len(mk)) if close >= 0 else n
        elif t == "softbreak":
            nl = content.find("\n", c)
            pos = nl if nl >= 0 else c
            out.append(_Char(" ", base + pos, base + pos + 1))
            c = pos + 1
        elif t == "hardbreak":
            nl = content.find("\n", c)
            c = (nl + 1) if nl >= 0 else n
        elif t == "link_open":
            idx = content.find("[", c)
            if idx >= 0:
                stack.append((base + idx, base + idx + 1))
                c = idx + 1
        elif t == "link_close":
            br = content.find("]", c)
            par = content.find(")", br if br >= 0 else c)
            cl_start = base + (br if br >= 0 else c)
            if par >= 0:
                c = par + 1
                cl_end = base + par + 1
            elif br >= 0:
                c = br + 1
                cl_end = base + br + 1
            else:
                cl_end = base + c
            if stack:
                o0, o1 = stack.pop()
                regions.append(_Region(o0, o1, cl_start, cl_end))
        elif t == "image":
            par = content.find(")", c)
            c = (par + 1) if par >= 0 else n
        elif t == "math_inline":
            d1 = content.find("$", c)
            d2 = content.find("$", d1 + 1) if d1 >= 0 else -1
            c = (d2 + 1) if d2 >= 0 else n
        # html_inline / unknown: emit nothing, leave cursor (html:false in
        # the editor means raw HTML arrives as text tokens, handled above).
    return out, regions


def _base_for_inline(body: str, line_off: list[int], tok: Token) -> int:
    """Source offset of an inline token's ``content``. markdown-it strips
    block markers (``# ``, ``> ``, ``- ``, ``1. ``) from ``content``;
    locating it inside the block's source lines recovers the marker width
    so headings/lists/quotes map to true columns."""
    m = tok.map
    if not m:
        idx = body.find(tok.content)
        return idx if idx >= 0 else 0
    line_start = line_off[m[0]] if m[0] < len(line_off) else 0
    line_end = line_off[m[1]] if m[1] < len(line_off) else len(body)
    idx = body.find(tok.content, line_start, line_end)
    return idx if idx >= 0 else line_start


def _render_with_map(body: str) -> tuple[str, list[_Char], list[_Region]]:
    """Render ``body`` (assumed LF-normalised) to the flat text domain that
    equals ``textBetween(0, end, ' ')`` over the editor's ProseMirror doc,
    with a per-character source map and the inline-run delimiter spans."""
    tokens = _MD.parse(body)
    line_off = _line_offsets(body)
    chars: list[_Char] = []
    regions: list[_Region] = []
    # A block separator is owed only AFTER a block that actually produced
    # rendered text — mirroring prosemirror ``Fragment.textBetween``, whose
    # ``separated`` flag is reset by real text/leaf output, never latched on
    # by an empty block. A paragraph that renders to zero chars (image-only
    # ``![](x)`` or inline-math-only ``$x$``) must NOT make ``emitted`` true,
    # or the next block emits a SECOND space where the editor's capture put
    # one, and every cross-figure suggestion then STALEs on accept.
    emitted = False
    for tok in tokens:
        if tok.type == "inline":
            base = _base_for_inline(body, line_off, tok)
            if emitted:
                chars.append(_Char(" ", base, base))
            before = len(chars)
            cells, regs = _render_inline(tok.content, tok.children or [], base)
            chars.extend(cells)
            regions.extend(regs)
            emitted = len(chars) > before
        elif tok.type in ("fence", "code_block"):
            # textBetween emits a code block's literal text (interior
            # newlines kept, trailing newline dropped) as one textblock.
            text = tok.content[:-1] if tok.content.endswith("\n") else tok.content
            start = line_off[tok.map[0]] if tok.map and tok.map[0] < len(line_off) else 0
            base = body.find(text, start) if text else start
            if base < 0:
                base = start
            if emitted:
                chars.append(_Char(" ", base, base))
            before = len(chars)
            for k, ch in enumerate(text):
                chars.append(_Char(ch, base + k, base + k + 1))
            emitted = len(chars) > before
        # horizontal_rule / math_block and other non-text blocks: emit
        # nothing and NO separator (matches textBetween).
    return "".join(c.ch for c in chars), chars, regions


def render_text(body: str) -> str:
    """The rendered-text projection of a markdown body, equal to the
    editor's ``doc.textBetween(0, end, ' ')``. Exposed for tests and for
    rendering the proposed replacement standalone."""
    norm, _ = _normalize(body)
    return _render_with_map(norm)[0]


@dataclass(frozen=True)
class Located:
    """Source span ``[src_start, src_end)`` of the ORIGINAL body to replace."""

    src_start: int
    src_end: int


def _locate(
    full: str, original: str, prefix: str | None, suffix: str | None
) -> tuple[int, int] | None:
    """Rendered-domain locate, mirroring the editor decoration and the old
    splice: an anchored (prefix+original+suffix) needle that occurs exactly
    once wins; else a unique bare ``original``; else None (ambiguous/gone)."""
    pfx = prefix or ""
    sfx = suffix or ""
    needle = pfx + original + sfx
    if needle != original and full.count(needle) == 1:
        i = full.find(needle) + len(pfx)
        return i, i + len(original)
    if full.count(original) == 1:
        i = full.find(original)
        return i, i + len(original)
    return None


def resolve_anchor(
    body: str,
    *,
    original: str,
    prefix: str | None,
    suffix: str | None,
    proposed: str,
) -> Located | None:
    """Resolve a suggestion anchor to a faithful source span, or None when
    it cannot be applied without ambiguity or corruption.

    Faithfulness is proven, not assumed: the candidate body must render to
    exactly ``head + render(proposed) + tail`` (the rendered text outside
    the span is byte-identical and the span became the proposed content).
    Any drift in the source map, a cross-mark/partial-link straddle, or a
    block-structure collapse fails this equality and yields None -> STALE.
    """
    if not original:
        return None
    norm, omap = _normalize(body)
    full, chars, regions = _render_with_map(norm)
    if len(chars) != len(full):  # invariant; defensive
        return None
    loc = _locate(full, original, prefix, suffix)
    if loc is None:
        return None
    s, e = loc
    if not (0 <= s < e <= len(chars)):
        return None
    src_start = chars[s].src_start
    src_end = chars[e - 1].src_end
    if not (0 <= src_start <= src_end <= len(norm)):
        return None
    # Edge-of-mark straddle repair: when the located source span covers exactly
    # one delimiter of an inline run (e.g. the opening ``**`` of a bold whose
    # content ends the selection, or the ``[`` of a link whose text starts it)
    # a naive contiguous splice leaves the partner delimiter orphaned, and the
    # re-render gate below would reject the (otherwise valid) edit. Grow the
    # span to swallow the whole run, dropping the now-meaningless formatting
    # (faithful in the rendered domain), until no run is half-covered. The gate
    # remains the arbiter, so an over-reach can only decline (STALE), never
    # corrupt.
    changed = True
    while changed:
        changed = False
        for reg in regions:
            open_in = src_start <= reg.open_start and reg.open_end <= src_end
            close_in = src_start <= reg.close_start and reg.close_end <= src_end
            if open_in != close_in:
                ns = min(src_start, reg.open_start)
                ne = max(src_end, reg.close_end)
                if ns != src_start or ne != src_end:
                    src_start, src_end = ns, ne
                    changed = True
    if not (0 <= src_start <= src_end <= len(norm)):
        return None
    prop_norm, _ = _normalize(proposed)
    candidate = norm[:src_start] + prop_norm + norm[src_end:]
    expected = full[:s] + render_text(proposed) + full[e:]
    if _render_with_map(candidate)[0] != expected:
        return None
    return Located(omap[src_start], omap[src_end])


def splice(
    body: str,
    *,
    original: str,
    proposed: str,
    prefix: str | None,
    suffix: str | None,
) -> str | None:
    """Apply ``original -> proposed`` to the markdown ``body`` at the
    resolved anchor. Returns the new body, or None when the anchor is no
    longer faithfully locatable (the caller raises SUGGESTION_STALE)."""
    loc = resolve_anchor(body, original=original, prefix=prefix, suffix=suffix, proposed=proposed)
    if loc is None:
        return None
    return body[: loc.src_start] + proposed + body[loc.src_end :]
