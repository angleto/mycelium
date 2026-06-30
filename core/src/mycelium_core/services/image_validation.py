"""Shared raster-image validation for stored binary images (issuer
letterhead logos and user avatars). One place for the size cap, the accepted
MIME allowlist, and the "is this actually a decodable raster?" gate, so a
non-image blob can never be persisted and then 500 a later render.
"""

from __future__ import annotations

# Cap a stored image (logo / avatar) so a row never carries an oversized blob;
# a QR/mycelium PNG or a letterhead logo sits comfortably under this.
IMAGE_MAX_BYTES = 512 * 1024
# Client-declared MIME allowlist (never sniffed): the raster formats reportlab
# can draw into a PDF and a browser can render directly.
IMAGE_MIMES = frozenset({"image/png", "image/jpeg"})


def image_is_decodable(data: bytes) -> bool:
    """True iff ``data`` is a FULLY decodable raster (header AND pixels).
    ``getSize`` only parses the header; ``getRGBData`` forces the full pixel
    decode, so a truncated/corrupt body is caught here instead of later at
    draw time (which would crash a PDF render). Reportlab's ImageReader
    delegates to PIL when present, else its own PNG reader. Reportlab is
    imported lazily so importing this module stays cheap on hot paths."""
    from io import BytesIO

    from reportlab.lib.utils import ImageReader

    try:
        reader = ImageReader(BytesIO(data))
        if not all(reader.getSize()):
            return False
        reader.getRGBData()
    except Exception:
        return False
    return True
