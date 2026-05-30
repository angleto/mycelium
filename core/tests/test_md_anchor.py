"""Unit tests for the markdown-aware suggestion anchor resolver.

Pure (no DB): they pin ``render_text`` against the editor's
``doc.textBetween(0, end, ' ')`` oracle and exercise ``splice`` across
inline formatting, links, math, code, tables, multi-block selections and
the corruption-refusing STALE path.
"""

from __future__ import annotations

import pytest

from flow_core.services import md_anchor


def _sp(body: str, original: str, proposed: str, prefix=None, suffix=None) -> str | None:
    return md_anchor.splice(
        body, original=original, proposed=proposed, prefix=prefix, suffix=suffix
    )


# --------------------------------------------------------------------------
# render_text: must equal ProseMirror textBetween(0, end, ' ') for the
# editor's construct set (oracle-pinned values captured from @tiptap/pm).
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("para1\n\npara2", "para1 para2"),  # block join
        ("# Head\n\nbody text", "Head body text"),  # heading marker stripped
        ("a **bold** c", "a bold c"),
        ("a _em_ b", "a em b"),
        ("a ~~gone~~ b", "a gone b"),
        ("x `code` y", "x code y"),
        ("before ![alt](u) after", "before  after"),  # image -> 0 chars (double space)
        ("energy is $E=mc^2$ exactly", "energy is  exactly"),  # math atom -> 0 chars
        ("line one  \nline two", "line oneline two"),  # hardbreak: no space
        ("a\nb", "a b"),  # softbreak: single space
        ("see [docs](http://x) ok", "see docs ok"),  # link -> text only
        ("ping [Alice](@note:abc-123) ok", "ping Alice ok"),  # mention link
        ("x `  spaced  ` y", "x  spaced  y"),  # code keeps one boundary space
        ("- one\n- two", "one two"),  # list items joined
        ("> quoted\n\nplain", "quoted plain"),  # blockquote
        ("| a | b |\n|---|---|\n| one | two |", "a b one two"),  # table cells
        ("intro\n\n```\nx = 1\n```\n\nend", "intro x = 1 end"),  # fenced code
    ],
)
def test_render_text_matches_oracle(body: str, expected: str) -> None:
    assert md_anchor.render_text(body) == expected


# --------------------------------------------------------------------------
# splice: faithful across formatting + multi-block.
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("body", "original", "proposed", "expected"),
    [
        # plain regression (the existing happy path)
        ("The quick brown fox jumps.", "quick brown fox", "lazy dog", "The lazy dog jumps."),
        # inline marks: delimiters preserved, only inner text replaced
        ("a **bold** c", "bold", "strong", "a **strong** c"),
        ("a _em_ b", "em", "EM", "a _EM_ b"),
        ("a ~~gone~~ b", "gone", "kept", "a ~~kept~~ b"),
        ("x `code` y", "code", "XX", "x `XX` y"),
        # link / mention: URL/href preserved, only the link text changes
        ("see [docs](http://x) ok", "docs", "HERE", "see [HERE](http://x) ok"),
        ("ping [Alice](@note:abc-123) ok", "Alice", "Bob", "ping [Bob](@note:abc-123) ok"),
        # heading keeps its marker
        ("# Title\n\nbody", "Title", "New", "# New\n\nbody"),
        # multi-block selection now applies (rendered-faithful)
        ("para1\n\npara2", "para1 para2", "MERGED", "MERGED"),
        # CRLF preserved elsewhere; the spliced region resolves
        ("para1\r\n\r\npara2", "para1 para2", "X", "X"),
        # editor-escaped bare star: render unescapes, splice over the run
        ("rate 5 \\* 3 done", "rate 5 * 3 done", "ok", "ok"),
        # proposed may carry markdown (inserted verbatim, gate validates)
        ("a plain b", "plain", "**bold**", "a **bold** b"),
        # table cell
        (
            "| a | b |\n|---|---|\n| one | two |",
            "two",
            "TWO",
            "| a | b |\n|---|---|\n| one | TWO |",
        ),
        # text adjacent to a code block, fence untouched
        (
            "intro\n\n```\nx = 1\n```\n\nbody here",
            "body here",
            "DONE",
            "intro\n\n```\nx = 1\n```\n\nDONE",
        ),
    ],
)
def test_splice_faithful(body: str, original: str, proposed: str, expected: str) -> None:
    assert _sp(body, original, proposed) == expected


def test_splice_repeated_text_disambiguated_by_suffix() -> None:
    body = "see the report now\n\nsee the report later"
    # the second 'report' is pinned by its rendered suffix
    assert _sp(body, "report", "REVIEW", suffix=" later") == (
        "see the report now\n\nsee the REVIEW later"
    )


# --------------------------------------------------------------------------
# splice: declines (STALE) instead of corrupting.
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("body", "original", "proposed"),
    [
        # target text simply absent
        ("hello world", "not here", "X"),
        # repeated text with no disambiguating anchor
        ("see the report\n\nsee the report", "report", "X"),
        # straddle across a mark boundary would orphan the ** -> refuse
        ("a **b** c", "b c", "X Y"),
        # the quote is inside the URL, not the rendered text -> refuse
        ("visit [here](http://example.com/foobar) ok", "foobar", "X"),
        # empty original is never a valid suggestion target
        ("anything", "", "X"),
    ],
)
def test_splice_declines_to_stale(body: str, original: str, proposed: str) -> None:
    assert _sp(body, original, proposed) is None


def test_splice_never_corrupts_on_unmodellable_input() -> None:
    # Even if the resolver cannot model a construct, it must return either a
    # body that renders to exactly the intended replacement, or None — never
    # a corrupted body. Exhaustively: any non-None result is render-faithful.
    body = "a **b _c_ d** e [f](u) g"
    for original in ("b", "c", "f", "b c d", "f g"):
        out = _sp(body, original, "Z")
        if out is not None:
            head, _, tail = md_anchor.render_text(body).partition(original)
            assert md_anchor.render_text(out) == head + "Z" + tail
