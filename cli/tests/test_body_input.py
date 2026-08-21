"""The CLI sends the markdown bytes it was given.

``docs/markdown-syntax.md`` promises it, and the ``edit`` subcommands
honoured it, but the ``create`` and ``comment`` ones ran
``.strip() or None`` on stdin and on the ``$EDITOR`` buffer. Two
consequences, both silent:

- a body opening with a 4-space indented code block lost the block, and a
  trailing two-space hard break was eaten;
- the same text meant different bytes depending on how it arrived
  (``note create -`` stripped, ``note edit -`` did not).

``ui.body_or_none`` is now the single rule: blank means "nothing was
written", anything else is markdown and travels verbatim. Pure, no
backend.
"""

from __future__ import annotations

import pytest

from mycelium_cli.ui import body_or_none


@pytest.mark.parametrize(
    "raw",
    [
        "    indented code block\n\ntesto\n",
        "testo  \n",  # trailing two-space hard break
        "\n\n# Titolo\n",  # leading blank lines
        "\ttab-led\n",
        "  spazi ai bordi  ",
        "riga\r\nriga\r\n",  # CRLF survives too
    ],
)
def test_a_body_with_content_travels_verbatim(raw: str) -> None:
    assert body_or_none(raw) == raw


@pytest.mark.parametrize("raw", ["", "   ", "\n", "\n\n\t \n", "\r\n"])
def test_a_blank_body_reads_as_nothing_written(raw: str) -> None:
    """``$EDITOR`` quit without saving, an empty pipe, a buffer of
    whitespace: all of them mean "the user wrote nothing", which is what
    the call sites turn into an abort or an omitted field."""
    assert body_or_none(raw) is None


def test_none_passes_through() -> None:
    """``--body`` not supplied at all is not the same question, and stays
    ``None`` without touching the filesystem or the terminal."""
    assert body_or_none(None) is None
