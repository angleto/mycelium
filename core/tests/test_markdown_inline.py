"""Markdown built by interpolation stays markdown.

Every surface hands callers a paste-ready ``[label](href)`` reference, and
the label is user data: an uploaded filename, an entity title. None of the
three emitters escaped it, so an attachment called ``Report ]final.pdf``
produced ``[Report ]final.pdf](/attachments/<id>/download)``, which is not
a link at all: CommonMark reads it as the link ``[Report ]`` followed by
the literal text ``final.pdf](/attachments/...)``. The backend's
``_sanitize_filename`` strips path separators and leading dots only, so
brackets and backslashes reach the emitter untouched.

Pure unit tests: no DB, no settings.
"""

from __future__ import annotations

import pytest

from mycelium_core.markdown_inline import md_link, md_link_destination, md_link_label


@pytest.mark.parametrize(
    ("raw", "escaped"),
    [
        ("Report ]final.pdf", r"Report \]final.pdf"),
        ("[draft] notes.md", r"\[draft\] notes.md"),
        (r"back\slash.txt", r"back\\slash.txt"),
        # Backslash first, or the escape we add would itself be escaped and
        # the bracket would come back out unescaped.
        (r"weird\].pdf", r"weird\\\].pdf"),
        ("plain name.pdf", "plain name.pdf"),
        ("accenti àèìòù 中文.pdf", "accenti àèìòù 中文.pdf"),
    ],
)
def test_label_escaping(raw: str, escaped: str) -> None:
    assert md_link_label(raw) == escaped


def test_label_collapses_newlines() -> None:
    """A blank line inside a label ends the paragraph and truncates the
    link. Titles can carry newlines; references are one-liners."""
    assert md_link_label("titolo\n\nsu due righe") == "titolo su due righe"
    assert md_link_label("titolo\r\nsu due righe") == "titolo su due righe"


def test_destination_left_alone_when_already_safe() -> None:
    """Every destination emitted today is safe, and must not grow angle
    brackets it does not need: the SPA matches the bare path."""
    assert md_link_destination("/attachments/abc-123/download") == "/attachments/abc-123/download"
    assert md_link_destination("@task:0f9c") == "@task:0f9c"


def test_destination_wrapped_when_it_would_break() -> None:
    assert md_link_destination("/a b/c.png") == "</a b/c.png>"
    assert md_link_destination("/a(b).png") == "</a(b).png>"
    assert md_link_destination("/a<b>.png") == r"</a\<b\>.png>"


def test_md_link_shapes() -> None:
    assert md_link("name.pdf", "/attachments/x/download") == "[name.pdf](/attachments/x/download)"
    assert (
        md_link("shot.png", "/attachments/x/download", image=True)
        == "![shot.png](/attachments/x/download)"
    )


def test_escaped_reference_survives_a_markdown_parse() -> None:
    """The point of the escaping, stated as the property that matters: the
    emitted string parses as ONE link whose text is the original filename."""
    markdown_it = pytest.importorskip("markdown_it")
    md = markdown_it.MarkdownIt()
    name = "Report ]final[.pdf"
    tokens = md.parse(md_link(name, "/attachments/x/download"))
    inline = [t for t in tokens if t.type == "inline"]
    assert len(inline) == 1
    children = inline[0].children or []
    kinds = [c.type for c in children]
    assert kinds == ["link_open", "text", "link_close"], kinds
    assert children[1].content == name
    assert dict(children[0].attrs)["href"] == "/attachments/x/download"
