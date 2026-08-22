"""Source-domain anchoring: locate, convert, and splice without corrupting.

The markdown editor's document IS the markdown, so an annotation's anchor is
a span of the source and locating it is ``str.find``. What still has to be
defended is the SPLICE: an agent can propose any string through MCP, and one
that changes the document's BLOCK STRUCTURE corrupts it even though it
applies cleanly. The rendered path caught those as a side effect of its
re-render equality gate; the replacement is an explicit structural check, and
these tests are its specification.

Pure unit tests: no DB, no settings.
"""

from __future__ import annotations

import pytest

from mycelium_core.services.md_anchor import (
    block_shape,
    locate_source_span,
    source_quote_for,
    splice_source,
)

TABLE = "| a | b |\n| --- | --- |\n| 1 | 2 |\n"
LIST = "- primo\n- secondo\n- terzo\n"


def _loc(body: str, original: str, prefix: str | None = None, suffix: str | None = None):
    return locate_source_span(body, original=original, prefix=prefix, suffix=suffix)


class TestLocate:
    def test_finds_a_unique_quote(self) -> None:
        body = "un **grassetto** qui\n"
        assert _loc(body, "**grassetto**") == (3, 16)
        assert body[3:16] == "**grassetto**"

    def test_quotes_the_markup_because_the_domain_is_the_source(self) -> None:
        # The whole point: an agent reads `**grassetto**` and quotes it. In
        # the rendered domain that quote did not exist at all.
        body = "un **grassetto** qui\n"
        assert _loc(body, "grassetto") is not None
        assert _loc(body, "**grassetto**") is not None

    def test_context_disambiguates_a_repeated_quote(self) -> None:
        body = "primo caso\n\nsecondo caso\n"
        assert _loc(body, "caso") is None
        s, e = _loc(body, "caso", prefix="secondo ", suffix="\n")  # type: ignore[misc]
        assert body[s:e] == "caso"
        assert s > body.index("secondo")

    def test_declines_rather_than_guessing(self) -> None:
        assert _loc("a\n\na\n", "a") is None
        assert _loc("niente qui\n", "assente") is None
        assert _loc("qualsiasi\n", "") is None


class TestConversion:
    """What migration 0099 does to every existing row."""

    def test_a_rendered_quote_becomes_the_source_it_covers(self) -> None:
        body = "Il termine **importante** va spiegato.\n"
        # The SPA captured the rendered text, which has no asterisks.
        got = source_quote_for(body, original="importante", prefix="Il termine ", suffix=" va")
        assert got is not None
        quote, prefix, suffix = got
        # Deterministic, not a guess: resolve_anchor already returns source
        # offsets, so the converted quote is the source those offsets cover.
        assert quote in body
        assert body[body.index(quote) - len(prefix) : body.index(quote)] == prefix
        # And the converted triple locates in the SOURCE domain.
        assert locate_source_span(body, original=quote, prefix=prefix, suffix=suffix) is not None

    def test_a_link_label_converts_to_the_whole_link(self) -> None:
        body = "vedi [la guida](https://example.com) per il resto\n"
        got = source_quote_for(body, original="la guida", prefix="vedi ", suffix=" per")
        assert got is not None
        assert "https://example.com" in got[0] or got[0] == "la guida"

    def test_an_unresolvable_anchor_converts_to_nothing(self) -> None:
        # The row stays in the rendered domain, which is honest: it was
        # already un-paintable for the same reason it fails here.
        assert source_quote_for("testo\n", original="sparito", prefix=None, suffix=None) is None


class TestSpliceStructuralGate:
    def test_applies_an_ordinary_inline_replacement(self) -> None:
        body = "un **grassetto** qui\n"
        assert (
            splice_source(body, original="grassetto", proposed="corsivo", prefix=None, suffix=None)
            == "un **corsivo** qui\n"
        )

    def test_refuses_a_pipe_that_adds_a_table_cell(self) -> None:
        # Reachable from MCP: the row would gain a cell and GFM drops the
        # extra one, destroying data on accept.
        assert (
            splice_source(TABLE, original="1", proposed="1 | X", prefix=None, suffix=None) is None
        )

    def test_allows_a_plain_word_inside_a_table_cell(self) -> None:
        assert (
            splice_source(TABLE, original="1", proposed="uno", prefix=None, suffix=None)
            == "| a | b |\n| --- | --- |\n| uno | 2 |\n"
        )

    def test_refuses_a_blank_line_that_splits_a_list_item(self) -> None:
        assert (
            splice_source(
                LIST, original="secondo", proposed="secondo\n\nintruso", prefix=None, suffix=None
            )
            is None
        )

    def test_refuses_a_fence_run_inside_a_fence(self) -> None:
        body = "```\nlet x = 1\n```\n"
        assert (
            splice_source(
                body, original="let x = 1", proposed="```\nrotto", prefix=None, suffix=None
            )
            is None
        )

    def test_refuses_a_heading_promotion(self) -> None:
        body = "testo normale\n"
        assert (
            splice_source(
                body, original="testo normale", proposed="# Titolo", prefix=None, suffix=None
            )
            is None
        )

    def test_refuses_when_the_anchor_is_ambiguous(self) -> None:
        assert (
            splice_source("a\n\na\n", original="a", proposed="b", prefix=None, suffix=None) is None
        )

    def test_keeps_edge_whitespace_because_it_is_markdown(self) -> None:
        # The rendered path stripped the proposal's edges. Here a two-space
        # hard break is content, and the splice must carry it through.
        body = "riga uno\nriga due\n"
        out = splice_source(
            body, original="riga uno", proposed="riga uno  ", prefix=None, suffix=None
        )
        assert out == "riga uno  \nriga due\n"

    def test_the_residual_it_does_NOT_catch_is_still_valid_markdown(self) -> None:
        # Named in the docstring rather than left to be discovered: a quote
        # that splits an inline delimiter run keeps the block shape, so the
        # gate passes. The result renders oddly but is valid markdown, which
        # is the guarantee this codebase makes.
        body = "**bold** and more\n"
        out = splice_source(body, original="ld** and", proposed="X", prefix=None, suffix=None)
        assert out == "**boX more\n"


class TestBlockShape:
    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("uno **due** tre\n", "uno due tre\n"),
            ("un [link](http://e.com)\n", "un altro testo\n"),
            (TABLE, "| a | b |\n| --- | --- |\n| x | y |\n"),
        ],
    )
    def test_inline_changes_keep_the_shape(self, a: str, b: str) -> None:
        assert block_shape(a) == block_shape(b)

    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("paragrafo\n", "# heading\n"),
            (LIST, "- primo\n\n- secondo\n\n- terzo\n"),
            (TABLE, TABLE + "\n| extra |\n| --- |\n| x |\n"),
            ("testo\n", "testo\n\n```\ncode\n```\n"),
        ],
    )
    def test_structural_changes_do_not(self, a: str, b: str) -> None:
        assert block_shape(a) != block_shape(b)
