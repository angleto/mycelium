"""The edge answers unknown ``/.well-known/*`` paths with 404 (ADR-0069).

Before, ``/.well-known/openid-configuration`` fell through to the SPA's
``try_files`` and came back 200 text/html, which an MCP client probing for
metadata parses as a broken document instead of moving on.

This is a static check of ``docker/nginx.conf``, and that is its limit: it
asserts the blocks exist, not how nginx resolves them. The resolution was
verified by running the image's nginx on this file (2026-11-13): with the
old file ``/.well-known/openid-configuration`` answered 200 text/html, with
this one 404, while the two OAuth paths were still proxied to the backend.
"""

from __future__ import annotations

import re
from pathlib import Path

_CONF = Path(__file__).resolve().parents[2] / "docker" / "nginx.conf"


def _blocks() -> dict[str, str]:
    text = _CONF.read_text(encoding="utf-8")
    return {
        m.group(1).strip(): m.group(2)
        for m in re.finditer(r"^[ \t]*location\s+([^{\n]+)\{([^}]*)\}", text, re.MULTILINE)
    }


def test_unknown_well_known_paths_are_404() -> None:
    blocks = _blocks()
    assert "/.well-known/" in blocks
    assert re.search(r"\breturn\s+404\s*;", blocks["/.well-known/"])


def test_the_oauth_metadata_paths_are_still_proxied() -> None:
    blocks = _blocks()
    for path in (
        "/.well-known/oauth-authorization-server",
        "/.well-known/oauth-protected-resource",
    ):
        assert path in blocks, path
        assert "proxy_pass" in blocks[path], path
        # A longer prefix than the catch-all, so nginx picks it for its path.
        assert len(path) > len("/.well-known/")


def test_the_spa_document_cannot_be_framed_by_another_site() -> None:
    """The consent page's Approve button mints a credential: a foreign page
    must not be able to frame the SPA and steer a click onto it. Every SPA
    route is served as /index.html (try_files), so the headers live there.
    'self', not 'none': the app frames its own blob: PDF previews.

    Verified on nginx:alpine with this file (2026-11-13): GET /oauth/consent
    carries both headers; with the previous file it carried neither."""
    block = _blocks()["= /index.html"]
    assert re.search(r"Content-Security-Policy\s+\"frame-ancestors 'self'\"", block)
    assert re.search(r"X-Frame-Options\s+\"SAMEORIGIN\"", block)
