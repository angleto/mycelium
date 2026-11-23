"""What counts as a reference written in a body (ADR-0073 decision 2).

One rule, in one place, with no database: given a markdown body, return
every reference a person wrote in it, with where it sits. Resolving a
reference to an entity is :mod:`mycelium_core.services.body_references`'s
job; this module only reads text.

Three syntaxes are recognised, the three a body can carry today:

``mention``
    A markdown link whose target is ``@task:<uuid>``, ``@note:<uuid>`` or
    ``@tag:<uuid>``, the shape the ``@`` typeahead inserts
    (``web/src/lib/mentions.ts``). The kind is written, so it is known
    before resolution.
``chip``
    An inline code span whose whole content is a UUID prefix: 4 to 36
    characters of hex and dashes, starting and ending on a hex digit, the
    ADR-0038 convention the ``[[`` typeahead inserts and that the renderer
    turns into a chip (``web/src/shared/prefix.ts``). The kind is unknown
    until resolution.
``bare``
    An 8-hex code or a full UUID typed in prose, under the gate of the
    normaliser that used to backtick them (``normalize_note_uuids``):
    never inside code, a link, an autolink or a bare URL, and bounded so it
    is never a run inside a longer hex or dashed token. A bare token is a
    reference only if it resolves, which is decided later; here it is a
    candidate.

The structure comes from the same markdown-it configuration
:mod:`md_anchor` uses to mirror the editor, so a fenced block, an indented
code block, inline code and a link are what the editor says they are, not
what a regex guesses. Source offsets are in the LF-normalised body
(:func:`md_anchor.normalise_line_endings`), and every reference carries a
W3C text-quote anchor in the RENDERED domain: the text a reader sees, with
enough context on each side to show the sentence it sits in.

The renderer has its own recogniser, because it decorates text that has not
been saved. The two are held together by the shared fixture corpus
``web/src/shared/fixtures/body_references.json``, which the Python suite runs
against this module and the web suite against the renderer's predicates.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal

from markdown_it.token import Token

from mycelium_core.services.md_anchor import (
    normalise_line_endings,
    parse,
    rendered_source_spans,
)

Syntax = Literal["mention", "chip", "bare"]
MentionKind = Literal["task", "note", "tag"]

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_MENTION_HREF_RE = re.compile(rf"^@(task|note|tag):({_UUID})$", re.IGNORECASE)
# web/src/shared/prefix.ts HEX_PREFIX_RE, and lookup._PREFIX_RE.
_CHIP_RE = re.compile(r"^[0-9a-f][0-9a-f-]{2,34}[0-9a-f]$", re.IGNORECASE)
# normalize_note_uuids._TOKEN_RE: a full UUID or an 8-hex run, never a
# sub-run of a longer hex, dashed or word token.
_BARE_RE = re.compile(
    r"(?<![0-9a-zA-Z`_-])"
    rf"({_UUID}|[0-9a-f]{{8}})"
    r"(?![0-9a-zA-Z`_-])",
    re.IGNORECASE,
)
# A bare URL in prose (linkify is off in the editor, so it arrives as text).
_URL_RE = re.compile(r"https?://\S+")

# Rendered context kept on each side of a reference: enough for the
# sentence around it, bounded so a reference in a long paragraph does not
# store the paragraph.
CONTEXT_CHARS = 160
_SENTENCE_END_RE = re.compile(r"[.!?…](?=\s)")


@dataclass(frozen=True, slots=True)
class Anchor:
    """W3C text-quote selector in the rendered domain."""

    exact: str
    prefix: str
    suffix: str


@dataclass(frozen=True, slots=True)
class Reference:
    """One occurrence of a reference in a body.

    ``token`` is what identifies the target as written: the lowercased UUID
    of a mention, the lowercased prefix of a chip or a bare code.
    ``ordinal`` counts earlier occurrences of the same ``(syntax, token)``
    in the same body, so an occurrence keeps its identity when text around
    it changes. ``start``/``end`` delimit the whole written form (the
    backticks of a chip, ``[label](@kind:id)`` of a mention) in the
    LF-normalised body."""

    syntax: Syntax
    token: str
    ordinal: int
    mention_kind: MentionKind | None
    start: int
    end: int
    anchor: Anchor | None


@dataclass(frozen=True, slots=True)
class _Found:
    syntax: Syntax
    token: str
    mention_kind: MentionKind | None
    start: int
    end: int


def _line_offsets(body: str) -> list[int]:
    offs = [0]
    for i, ch in enumerate(body):
        if ch == "\n":
            offs.append(i + 1)
    return offs


def _inline_base(body: str, line_off: list[int], tok: Token) -> int:
    """Source offset of an inline token's ``content`` (block markers such as
    ``# ``, ``> ``, ``- `` are stripped from it by the parser). Same
    recovery as ``md_anchor._base_for_inline``."""
    m = tok.map
    if not m:
        idx = body.find(tok.content)
        return idx if idx >= 0 else 0
    line_start = line_off[m[0]] if m[0] < len(line_off) else 0
    line_end = line_off[m[1]] if m[1] < len(line_off) else len(body)
    idx = body.find(tok.content, line_start, line_end)
    return idx if idx >= 0 else line_start


def _text_offsets(content: str, cursor: int, text: str) -> tuple[list[int], int]:
    """Source offsets (relative to ``content``) of each character of a text
    token, honouring backslash escapes, and the cursor after it. Mirrors the
    text branch of ``md_anchor._render_inline``."""
    out: list[int] = []
    c = cursor
    n = len(content)
    for char in text:
        if c < n and content[c] == "\\" and c + 1 < n and content[c + 1] == char:
            out.append(c + 1)
            c += 2
        elif c < n and content[c] == char:
            out.append(c)
            c += 1
        else:
            out.append(min(c, n))
            c = min(c + 1, n)
    return out, c


def _bare_in_text(text: str, offsets: list[int], base: int) -> Iterator[_Found]:
    masked = _URL_RE.sub(lambda m: "\x00" * len(m.group(0)), text)
    for m in _BARE_RE.finditer(masked):
        s, e = m.start(1), m.end(1)
        start = base + offsets[s]
        end = base + offsets[e - 1] + 1
        yield _Found("bare", m.group(1).lower(), None, start, end)


def _scan_inline(content: str, children: list[Token], base: int) -> Iterator[_Found]:
    """Walk one block's inline children with a cursor into its source,
    yielding the references it carries. The cursor discipline is
    ``md_anchor._render_inline``'s; where it cannot be exact (an entity
    decoded by the parser) the drift only affects the offsets of the bare
    codes after it, which are hex and contain nothing to decode."""
    c = 0
    n = len(content)
    link_depth = 0
    link_start = 0
    link_href: str | None = None
    for child in children:
        t = child.type
        if t == "text":
            offsets, c_after = _text_offsets(content, c, child.content)
            if link_depth == 0:
                yield from _bare_in_text(child.content, offsets, base)
            c = c_after
        elif t == "code_inline":
            mk = child.markup or "`"
            o_start = content.find(mk, c)
            if o_start < 0:
                o_start = c
            inner = o_start + len(mk)
            close = content.find(mk, inner)
            end = (close + len(mk)) if close >= 0 else n
            code = child.content
            # A chip inside a link label is still a chip: both web
            # recognisers decorate inline code wherever it sits.
            if _CHIP_RE.match(code):
                yield _Found("chip", code.lower(), None, base + o_start, base + end)
            c = end
        elif t == "link_open":
            if link_depth == 0:
                if child.markup == "autolink":
                    idx = content.find("<", c)
                else:
                    idx = content.find("[", c)
                link_start = idx if idx >= 0 else c
                href = child.attrs.get("href")
                link_href = href if isinstance(href, str) else None
                c = link_start + 1
            link_depth += 1
        elif t == "link_close":
            link_depth = max(0, link_depth - 1)
            if child.markup == "autolink":
                gt = content.find(">", c)
                c = (gt + 1) if gt >= 0 else n
            else:
                br = content.find("]", c)
                par = content.find(")", br if br >= 0 else c)
                c = (par + 1) if par >= 0 else ((br + 1) if br >= 0 else c)
            if link_depth == 0 and link_href is not None:
                m = _MENTION_HREF_RE.match(link_href.strip())
                if m is not None:
                    kind: MentionKind = m.group(1).lower()  # type: ignore[assignment]
                    yield _Found("mention", m.group(2).lower(), kind, base + link_start, base + c)
                link_href = None
        elif t == "softbreak" or t == "hardbreak":
            nl = content.find("\n", c)
            c = (nl + 1) if nl >= 0 else c
        elif t == "image":
            par = content.find(")", c)
            c = (par + 1) if par >= 0 else n
        elif t == "math_inline":
            d1 = content.find("$", c)
            d2 = content.find("$", d1 + 1) if d1 >= 0 else -1
            c = (d2 + 1) if d2 >= 0 else n
        elif t == "html_inline":
            idx = content.find(child.content, c)
            c = (idx + len(child.content)) if idx >= 0 else c
        elif t.endswith("_open") or t.endswith("_close"):
            mk = child.markup or ""
            if mk and content[c : c + len(mk)] == mk:
                c += len(mk)
            elif mk:
                idx = content.find(mk, c)
                c = (idx + len(mk)) if idx >= 0 else c


def _anchor(rendered: str, spans: list[tuple[int, int]], start: int, end: int) -> Anchor | None:
    """The rendered quote of the source span ``[start, end)`` with the
    sentence it sits in as prefix and suffix."""
    idx = [i for i, (s, e) in enumerate(spans) if e > s and start <= s and e <= end]
    if not idx:
        return None
    rs, re_ = idx[0], idx[-1] + 1
    # The context stays inside the reference's own block: a block separator
    # is the one rendered character with an empty source span.
    lo = rs
    while lo > 0 and rs - lo < CONTEXT_CHARS and spans[lo - 1][1] > spans[lo - 1][0]:
        lo -= 1
    hi = re_
    while hi < len(spans) and hi - re_ < CONTEXT_CHARS and spans[hi][1] > spans[hi][0]:
        hi += 1
    before = rendered[lo:rs]
    after = rendered[re_:hi]
    ends_before = list(_SENTENCE_END_RE.finditer(before))
    if ends_before:
        before = before[ends_before[-1].end() :]
    end_after = _SENTENCE_END_RE.search(after)
    if end_after is not None:
        after = after[: end_after.end()]
    return Anchor(exact=rendered[rs:re_], prefix=before.lstrip(), suffix=after.rstrip())


def extract_references(body: str) -> list[Reference]:
    """Every reference written in ``body``, in reading order.

    Idempotent and pure: the same body always yields the same list, and a
    body with no reference yields an empty one. Fenced and indented code
    blocks contribute nothing."""
    norm = normalise_line_endings(body or "")
    if not norm:
        return []
    tokens = parse(norm)
    line_off = _line_offsets(norm)
    found: list[_Found] = []
    for tok in tokens:
        if tok.type != "inline":
            continue
        base = _inline_base(norm, line_off, tok)
        found.extend(_scan_inline(tok.content, tok.children or [], base))
    if not found:
        return []
    # Reading order by where the written form starts: a mention whose label
    # holds a chip comes before the chip, as it does on the page.
    found.sort(key=lambda f: (f.start, f.syntax != "mention"))
    rendered, spans = rendered_source_spans(norm)
    seen: dict[tuple[str, str], int] = {}
    out: list[Reference] = []
    for f in found:
        key = (f.syntax, f.token)
        ordinal = seen.get(key, 0)
        seen[key] = ordinal + 1
        out.append(
            Reference(
                syntax=f.syntax,
                token=f.token,
                ordinal=ordinal,
                mention_kind=f.mention_kind,
                start=f.start,
                end=f.end,
                anchor=_anchor(rendered, spans, f.start, f.end),
            )
        )
    return out
