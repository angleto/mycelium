"""Unit coverage for the paste-ready markdown reference helper.

Pure function (no client / no server): an image attachment becomes an
inline embed (``![name](...)``), everything else a download link. Both
point at the bearer-auth /attachments/<id>/download route — never a
public url.
"""

from __future__ import annotations

from mycelium_cli.cmds._common import attachment_markdown_ref


def test_non_image_is_link() -> None:
    ref = attachment_markdown_ref(
        {"id": "abc-123", "filename": "report.pdf", "mime_type": "application/pdf"}
    )
    assert ref == "[report.pdf](/attachments/abc-123/download)"


def test_image_is_embed() -> None:
    ref = attachment_markdown_ref(
        {"id": "img-9", "filename": "pixel.png", "mime_type": "image/png"}
    )
    assert ref == "![pixel.png](/attachments/img-9/download)"


def test_missing_mime_defaults_to_link() -> None:
    ref = attachment_markdown_ref({"id": "z", "filename": "x.bin", "mime_type": None})
    assert ref == "[x.bin](/attachments/z/download)"


def test_missing_filename_falls_back() -> None:
    ref = attachment_markdown_ref({"id": "z", "mime_type": "application/octet-stream"})
    assert ref == "[file](/attachments/z/download)"


def test_bracketed_filename_stays_one_link() -> None:
    """``_sanitize_filename`` on the backend strips path separators and
    leading dots only, so a filename keeps its brackets. Interpolating it
    raw produced ``[Report ]final.pdf](...)``, which CommonMark reads as the
    link ``[Report ]`` followed by literal text. Mirrors
    ``mycelium_core.markdown_inline`` and ``web/src/lib/markdownInline.ts``.
    """
    ref = attachment_markdown_ref(
        {"id": "abc", "filename": "Report ]final[.pdf", "mime_type": "application/pdf"}
    )
    assert ref == r"[Report \]final\[.pdf](/attachments/abc/download)"


def test_backslash_in_filename_is_escaped_first() -> None:
    """Backslash before the brackets, or the escape added for a bracket
    would itself be escaped and the bracket would come back out bare."""
    ref = attachment_markdown_ref(
        {"id": "abc", "filename": r"weird\].pdf", "mime_type": "application/pdf"}
    )
    assert ref == r"[weird\\\].pdf](/attachments/abc/download)"


def test_newline_in_filename_collapses() -> None:
    """A blank line inside a label ends the paragraph and truncates the
    link."""
    ref = attachment_markdown_ref(
        {"id": "abc", "filename": "due\nrighe.pdf", "mime_type": "application/pdf"}
    )
    assert ref == "[due righe.pdf](/attachments/abc/download)"
