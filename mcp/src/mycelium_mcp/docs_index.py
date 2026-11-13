"""The maintained ``docs/`` tree, addressable by document AND by section.

``help`` used to answer a topic with a whole document, and a keyword that
named no file with the whole document that mentioned it most: 24-32k
characters (6-8k tokens) for a question one section answers. An agent pays
that on every lookup, and a client cannot ask for less. This module splits
each document at its markdown headings so ``help`` can answer with an
outline, one section, or the few sections a keyword actually lands in.

Pure functions over the filesystem: no settings, no database.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from pathlib import Path

#: A document up to this size is still answered whole: splitting a page that
#: fits in a screen costs the reader a second call for nothing.
FULL_DOC_CHARS = 6000
#: One section is capped here; past it the answer says it was cut.
SECTION_CAP = 8000
#: A keyword answer carries this many sections, each capped at KEYWORD_CAP.
KEYWORD_SECTIONS = 3
KEYWORD_CAP = 3000
_TITLE_WEIGHT = 20
_DOC_WEIGHT = 10

_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_ADR_DIR = "adr"


@dataclass(frozen=True)
class Section:
    title: str
    level: int
    text: str


def doc_paths(docs: Path) -> dict[str, Path]:
    """Every maintained document by its stem: the top level and ``adr/``.
    ADR stems are unique (they carry their number), so one namespace holds
    both."""
    out = {p.stem: p for p in docs.glob("*.md")}
    adr = docs / _ADR_DIR
    if adr.is_dir():
        for p in adr.glob("*.md"):
            if p.stem[:1].isdigit():
                out.setdefault(p.stem, p)
    return out


def resolve(names: list[str], topic: str) -> str | None:
    """The document a topic names: exact stem (case-insensitive), an ADR by
    its number (``43``, ``0043``, ``adr-0043``), else a close spelling."""
    key = topic.strip().lower().replace(" ", "-")
    lower = {n.lower(): n for n in names}
    if key in lower:
        return lower[key]
    num = key.removeprefix("adr-").removeprefix("adr")
    if num.isdigit():
        prefix = num.zfill(4) + "-"
        hits = [n for n in names if n.startswith(prefix)]
        if len(hits) == 1:
            return hits[0]
    close = difflib.get_close_matches(key, list(lower), n=1, cutoff=0.6)
    return lower[close[0]] if close else None


def split_sections(text: str) -> tuple[str, list[Section]]:
    """``(preamble, sections)``. A section runs from its heading to the next
    heading of the same or a higher level, so a ``##`` carries its ``###``.
    Lines inside fenced code are never headings (a shell comment is not)."""
    lines = text.splitlines(keepends=True)
    heads: list[tuple[int, int, str]] = []  # (line index, level, title)
    fenced = False
    for i, line in enumerate(lines):
        if _FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        m = _HEADING.match(line)
        if m:
            heads.append((i, len(m.group(1)), m.group(2)))
    preamble_end = heads[0][0] if heads else len(lines)
    preamble = "".join(lines[:preamble_end])
    sections: list[Section] = []
    for k, (start, level, title) in enumerate(heads):
        end = len(lines)
        for nxt, nlevel, _ in heads[k + 1 :]:
            if nlevel <= level:
                end = nxt
                break
        sections.append(Section(title=title, level=level, text="".join(lines[start:end])))
    return preamble, sections


def outline(sections: list[Section], max_level: int = 3) -> list[str]:
    """The headings a reader picks a section from, indented by level."""
    return ["  " * (s.level - 1) + s.title for s in sections if s.level <= max_level]


def find_section(sections: list[Section], query: str) -> Section | None:
    """The section a query names: a heading containing it (the shallowest,
    then the first), else a close spelling of a heading."""
    q = query.strip().lower()
    hits = [s for s in sections if q in s.title.lower()]
    if hits:
        return min(hits, key=lambda s: s.level)
    titles = {s.title.lower(): s for s in sections}
    close = difflib.get_close_matches(q, list(titles), n=1, cutoff=0.5)
    return titles[close[0]] if close else None


def capped(text: str, limit: int) -> tuple[str, bool]:
    """``text`` cut at ``limit`` on a line boundary, and whether it was cut."""
    if len(text) <= limit:
        return text, False
    cut = text.rfind("\n", 0, limit)
    return text[: cut if cut > 0 else limit], True


def keyword_sections(
    paths: dict[str, Path], term: str
) -> tuple[list[tuple[str, Section, int]], list[str]]:
    """The sections that mention ``term`` most, best first, and every
    document that mentions it at all. Scored on the section's own text, so
    the answer is the passage, not the page that happens to repeat a word."""
    t = term.strip().lower()
    scored: list[tuple[str, Section, int]] = []
    docs_hit: dict[str, int] = {}
    for name, path in sorted(paths.items()):
        try:
            body = path.read_text(encoding="utf-8")
        except OSError:
            continue
        total = body.lower().count(t)
        if not total:
            continue
        docs_hit[name] = total
        _, sections = split_sections(body)
        # Leaf-most scoring: a parent section contains its children, so
        # score each section on the text before its first sub-heading. Raw
        # counts favour long sections that merely repeat the word, so a
        # section whose heading -- or whose document's name -- carries the
        # term ranks first: that is where the topic is the subject.
        named_doc = t in name.lower()
        for s in sections:
            n = _own_text(s).lower().count(t)
            if n:
                n += _TITLE_WEIGHT * (t in s.title.lower()) + _DOC_WEIGHT * named_doc
                scored.append((name, s, n))
    scored.sort(key=lambda r: (-r[2], r[0], r[1].title))
    ranked_docs = sorted(docs_hit, key=lambda n: (-docs_hit[n], n))
    return scored, ranked_docs


def _own_text(s: Section) -> str:
    """A section's text up to its first sub-heading (outside fences)."""
    lines = s.text.splitlines(keepends=True)
    fenced = False
    for i, line in enumerate(lines[1:], start=1):
        if _FENCE.match(line):
            fenced = not fenced
            continue
        if not fenced and _HEADING.match(line):
            return "".join(lines[:i])
    return s.text
