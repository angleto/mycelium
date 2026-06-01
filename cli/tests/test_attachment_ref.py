"""Unit coverage for the paste-ready markdown reference helper.

Pure function (no client / no server): an image attachment becomes an
inline embed (``![name](...)``), everything else a download link. Both
point at the bearer-auth /attachments/<id>/download route — never a
public url.
"""

from __future__ import annotations

from flow_cli.cmds._common import attachment_markdown_ref


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
