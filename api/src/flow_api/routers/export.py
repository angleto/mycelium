"""Markdown editor → PDF export.

Vector PDF rendering via WeasyPrint. The SPA's RichEditor sends the
rendered editor HTML (tiptap output, attachment images already
inlined as data: URLs) plus a slugified ``title``; this router wraps
the body in a minimal HTML document, attaches the bundled
``static/print.css`` (which also pulls in ``katex/katex.min.css`` for
math), and streams back ``application/pdf`` bytes.

The CSS lives on disk so WeasyPrint resolves ``@font-face`` and
``@import`` URLs via ``file://`` rather than fetching anything from
the network — the export is fully offline and deterministic.
"""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from weasyprint import HTML  # type: ignore[import-untyped]

from flow_api.deps import TenantCtx, tenant_ctx

router = APIRouter(prefix="/export", tags=["export"])


# Resolved once: ``static/`` sits next to this package, beside the
# bundled KaTeX assets that ``print.css`` @imports.
_STATIC = Path(__file__).resolve().parents[1] / "static"
_PRINT_CSS = _STATIC / "print.css"

# Cap the inbound body so a runaway client can't OOM the renderer.
# 8 MiB covers very large notes with many inlined images.
_MAX_HTML_BYTES = 8 * 1024 * 1024


class PdfExportIn(BaseModel):
    """Inbound payload for /export/pdf.

    ``title`` becomes the PDF ``<title>`` and the ``Content-Disposition``
    filename. ``html`` is the editor body (no ``<html>`` / ``<head>``);
    the router builds the full document around it.
    """

    title: str = Field(min_length=1, max_length=240)
    html: str = Field(min_length=1)


def _slugify_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "-", name).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return (cleaned or "untitled")[:120]


def _wrap_html(title: str, body_html: str) -> str:
    """Wrap the editor body in a minimal HTML5 document.

    ``<title>`` drives the PDF title metadata; ``<h1 class="pdf-title">``
    is a styled cover heading that also feeds ``string-set: doc-title``
    so subsequent pages can reprint it in the running header.
    """
    safe_title = (
        title.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    return (
        "<!doctype html>"
        '<html lang="en"><head><meta charset="utf-8"/>'
        f"<title>{safe_title}</title>"
        "</head>"
        '<body><article class="pdf-doc">'
        f'<h1 class="pdf-title">{safe_title}</h1>'
        f"{body_html}"
        "</article></body></html>"
    )


@router.post(
    "/pdf",
    responses={
        200: {
            "content": {"application/pdf": {}},
            "description": "Rendered PDF bytes (attachment).",
        }
    },
)
async def export_pdf(
    payload: PdfExportIn,
    _ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> Response:
    """Render ``payload.html`` to a vector PDF and stream it back.

    The endpoint is per-tenant authenticated (the dependency runs the
    full identity + RLS check) but does not touch the database: the
    rendered HTML is the only input the SPA has just produced, and the
    PDF is not persisted.
    """
    html_size = len(payload.html.encode("utf-8"))
    if html_size > _MAX_HTML_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"html exceeds {_MAX_HTML_BYTES} bytes",
        )

    doc = _wrap_html(payload.title, payload.html)
    # ``base_url`` lets relative URLs inside body_html (rare; tiptap
    # emits absolute /attachments/... but we inline those client-side)
    # resolve sensibly without hitting the network.
    html = HTML(string=doc, base_url=str(_STATIC))
    buf = io.BytesIO()
    html.write_pdf(buf, stylesheets=[str(_PRINT_CSS)])
    pdf_bytes = buf.getvalue()

    filename = _slugify_filename(payload.title) + ".pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )
